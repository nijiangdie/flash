"""用本机大模型给职位打匹配度：推理只发生在这台机器上，不访问任何招聘平台。

分工：提示词和打分细则放在这里（要能版本管理、能一眼看到喂了什么），
n8n 只负责定时叫 `cli.py analyze` 跑一趟、再把结果攒成摘要发信。
`POST /api/analysis` 也照样留着——想在 n8n 画布里自己拼提示词的用法不受影响。
"""
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import db
from .config import (ANALYSIS_PROFILE_PATH, ANALYZE_BATCH_DEFAULT, JD_MAX_CHARS,
                     LLM_BASE_URL, LLM_MODEL, LLM_TEMPERATURE, LLM_TIMEOUT_S)

# 自述送进提示词的上限：写再多模型也记不住，超长还会挤掉正文
PROFILE_MAX = 1200
# 连着这么多条都没跑通，基本是配置或模型本身的问题，别再往下耗时间
FAILURES_STOP = 3

SYSTEM = """你是求职匹配度评估助手。只根据给出的「我的情况」和「职位信息」判断，
不要编造我没有写出来的能力，也不要因为职位描述写得热闹就凭空加分。
「我的情况」里没提到的，当作不确定处理，不要替我脑补，更不要因为脑补而给高分。
score 打分细则（0~100，只看这条职位本身）：
90 以上：核心技能全部命中，年限、学历、城市、薪资都合适
70~89：主要技能命中，有一两处缺口但短期能补
50~69：只有一半技能对得上，年限或转岗方向有明显差距
30~49：只有少量技能重叠，岗位要求明显不达标
30 以下：方向不对，不建议投
"""

JSON_HINT = """只输出一个 JSON 对象，不要解释文字，不要 Markdown 代码块：
{"score": 0到100的数字, "matched": ["命中的技能或条件"], "gaps": ["缺口"], "reasons": "两三句说明为什么是这个分"}
matched 和 gaps 各不超过 8 项，reasons 控制在 120 字以内。"""

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
# 自述模板里的待填空：【几年】、【例：8k】这一类。
_PLACEHOLDER_RE = re.compile(r"【[^】]*】")


def placeholders(text: str) -> list[str]:
    """自述文件里还没填的占位。"""
    return _PLACEHOLDER_RE.findall(text)


def load_profile(path=None, allow_placeholders: bool = False) -> str:
    """读本机自述文件（匹配度的尺子）。它不进库、不进导出、不发给任何平台。"""
    p = Path(path) if path else ANALYSIS_PROFILE_PATH
    if not p.exists():
        raise RuntimeError(
            f"缺少自述文件：{p}\n"
            "  匹配度得有个参照，请新建这个纯文本文件，一行一条写清楚：\n"
            "  - 会的东西和大致年限（例：Python 3 年，写过爬虫和 Flask 接口）\n"
            "  - 总工作年限、学历、期望城市、能接受的月薪下限\n"
            "  - 不想干的方向（例：长期出差、纯售后、大小周）\n"
            "  这个文件只留在本地 data/ 里，只发给本机模型。")
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) < 10:
        raise RuntimeError(f"{p} 还基本是空的，先写几行你的情况再跑。")
    out = text[:PROFILE_MAX]
    if not allow_placeholders:
        ph = placeholders(out)
        if ph:
            # 这条不能只警告不拦：9B 模型是拿这份自述当尺子的，尺子上还写着
            # 「总工作年限：【几年】」，算出来的分既不该存库，更不该拿去排序。
            raise RuntimeError(
                f"{p} 里还有 {len(ph)} 处【】没填，例如："
                + "、".join(x[:26] + "】" if len(x) > 26 else x for x in ph[:3]) + "。\n"
                "  填完再跑：匹配度是按这份自述打的分，占位没填等于尺子上没刻度。\n"
                "  想先看送出去的内容（不发消息、不写库）就加 --dry-run。")
    return out


def job_block(job: dict) -> str:
    """一条职位 -> 提示词里的那段文本；正文按上限截断，免得挤爆本地模型的上下文。"""
    jd = (job.get("description") or "").strip()
    if len(jd) > JD_MAX_CHARS:
        jd = jd[:JD_MAX_CHARS] + "…（正文过长，已截断）"
    lines = [
        f"平台：{job.get('platform') or ''}",
        f"标题：{job.get('title') or ''}",
        f"城市：{job.get('city') or ''} {job.get('district') or ''}".strip(),
        f"薪资：{job.get('salary_raw') or '未写'}",
        f"经验要求：{job.get('experience') or '未写'}",
        f"学历要求：{job.get('education') or '未写'}",
        f"技能标签：{' / '.join(job.get('skills') or []) or '未列'}",
        f"行业：{job.get('industry') or '未写'}",
        f"公司规模：{job.get('company_scale') or '未写'}",
        "职位描述：",
        jd or "（没有正文，只按上面列表页的信息判断）",
    ]
    return "\n".join(lines)


def build_messages(job: dict, profile: str) -> list[dict]:
    user = (f"我的情况：\n{profile}\n\n待评估职位：\n{job_block(job)}\n\n{JSON_HINT}")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def extract_json(text: str) -> dict:
    """从模型输出里抠出那个 JSON 对象：代码块包裹、前面带一句客套话都能处理。"""
    raw = _FENCE_RE.sub("", (text or "").strip(), count=2)
    start = raw.find("{")
    if start < 0:
        raise ValueError("模型没返回 JSON，只给了这段：" + raw[:80])
    in_str = esc = False
    depth = 0
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(raw[start:i + 1])
    raise ValueError("模型返回的 JSON 没收尾，可能被截断了")


def parse_result(text: str, job: dict, model: str) -> dict:
    """模型输出 -> 可以直接交给 db.save_analysis 的那份字段（校验在 db 层做）。"""
    data = extract_json(text)
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    if data.get("score") is None:
        raise ValueError("模型没给 score")
    return {
        "platform": job["platform"],
        "platform_job_id": job["platform_job_id"],
        "score": data.get("score"),
        "matched": data.get("matched"),
        "gaps": data.get("gaps"),
        "reasons": data.get("reasons"),
        "model": model,
    }


def _open(req, timeout: int):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"接口返回 {e.code}：{e.read().decode('utf-8', 'replace')[:200]}") from e
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(
            f"连不上 {req.full_url}（{e}）。检查 LM Studio 的本地服务是否已启动、端口对不对。"
        ) from e
    except ValueError as e:
        raise RuntimeError(f"{req.full_url} 返回的不是 JSON，不像 OpenAI 兼容接口。") from e


def list_models(url: str = LLM_BASE_URL) -> list[str]:
    data = _open(urllib.request.Request(url.rstrip("/") + "/models"), LLM_TIMEOUT_S)
    return [m.get("id") for m in (data.get("data") or []) if m.get("id")]


def resolve_model(url: str, model: str = "") -> str:
    """没指定模型就用 LM Studio 当前已加载的那个，省得每次去抄模型 id。"""
    if model:
        return model
    ids = list_models(url)
    if not ids:
        raise RuntimeError("LM Studio 里没有已加载的模型，先在它界面里加载一个（或 --model 指定 id）。")
    if len(ids) > 1:
        print(f"  LM Studio 里加载了 {len(ids)} 个模型，用第一个：{ids[0]}（要换用 --model）")
    return ids[0]


def post_chat(messages: list[dict], url: str, model: str,
              timeout: int = LLM_TIMEOUT_S) -> str:
    body = json.dumps({"model": model, "messages": messages,
                       "temperature": LLM_TEMPERATURE, "stream": False},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    data = _open(req, timeout)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise RuntimeError(f"模型返回的结构不认识：{str(data)[:200]}") from e
    if not (content or "").strip():
        raise RuntimeError("模型返回了空内容")
    return content


def analyze_jobs(jobs: list[dict], *, profile: str, url: str = LLM_BASE_URL,
                 model: str = "", dry_run: bool = False) -> dict:
    """逐条问模型、逐条写回 job_analysis；单条失败只跳过这条，连着三条失败才停手。"""
    ok = failed = streak = 0
    spent = 0.0
    for n, job in enumerate(jobs, 1):
        tag = (f"{job['platform']} #{job['platform_job_id']} "
               f"{job.get('title') or ''} · {job.get('city') or ''}")
        messages = build_messages(job, profile)
        if dry_run:
            print(f"[预览 {n}/{len(jobs)}] {tag}")
            for m in messages:
                print(f"--- {m['role']} ---\n{m['content']}\n")
            continue
        print(f"[分析 {n}/{len(jobs)}] {tag}")
        started = time.perf_counter()
        try:
            content = post_chat(messages, url, model)
            saved = db.save_analysis(parse_result(content, job, model))
        except (ValueError, RuntimeError) as e:
            failed += 1
            streak += 1
            took = time.perf_counter() - started
            spent += took
            print(f"  这条没存下来（{took:.1f} 秒）：{e}")
            if streak >= FAILURES_STOP:
                raise RuntimeError(
                    f"连着 {streak} 条都没跑通，先停手。多半是模型或接口配置的问题，"
                    "用 --dry-run 看看实际送出去的提示词，或换 --model。") from e
            continue
        streak = 0
        took = time.perf_counter() - started
        spent += took
        if saved is None:
            failed += 1
            print("  库里找不到这条职位，跳过。")
        else:
            ok += 1
            print(f"  → 匹配度 {saved['score']}（{took:.1f} 秒）")
    return {"analyzed": ok, "failed": failed, "seconds": round(spent, 1)}


def run(limit: int = ANALYZE_BATCH_DEFAULT, platform: str | None = None,
        redo: bool = False, *, url: str = LLM_BASE_URL, model: str = LLM_MODEL,
        profile_path=None, dry_run: bool = False) -> dict:
    """入口：挑一批「有正文、还没算过」的职位，跑完把分数写回本地库。"""
    # --dry-run 允许带着占位跑：预览只看提示词长什么样，不发消息也不写库
    profile = load_profile(profile_path, allow_placeholders=dry_run)
    jobs = db.pending_analysis(limit=limit, platform=platform, redo=redo)
    if not jobs:
        print("没有可分析的职位：要么都算过了，要么候选的职位还没抓到正文。")
        print("提示：`python cli.py detail --limit 5` 先给要投的那几条抓正文。")
        return {"analyzed": 0, "failed": 0, "seconds": 0.0}
    if dry_run:
        # 预览不碰接口，LM Studio 关着也能用，改提示词时靠这个看送出去的内容
        return analyze_jobs(jobs, profile=profile, dry_run=True)
    model = resolve_model(url, model)
    print(f"接口：{url}｜模型：{model}｜本次 {len(jobs)} 条（纯本地推理，不访问招聘平台）")
    res = analyze_jobs(jobs, profile=profile, url=url, model=model)
    done = res["analyzed"] + res["failed"]
    avg = res["seconds"] / done if done else 0.0
    print(f"[分析] 完成 {res['analyzed']} 条，失败 {res['failed']} 条，"
          f"推理一共 {res['seconds']:.0f} 秒（平均 {avg:.1f} 秒/条）。"
          "刷新筛选台页面就能按『匹配度从高到低』排。")
    return res
