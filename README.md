# LLM 接口性能测试工具

对 OpenAI 兼容的 `/v1/chat/completions` 接口做压测，测量 **TTFT（首 token 延迟）、输出速度（tok/s）、整体延迟、缓存命中率**，支持多模型配置与对比。纯本地运行，API Key 只保存在本机 `data/profiles.json`。

## 启动

```bash
cd llm-bench
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # 首次
.venv/bin/python app.py
# 打开 http://127.0.0.1:8765
```

## 使用流程

1. **添加模型**：选提供商即可，不用填 base_url：
   - **DeepSeek**：只需 API Key，模型固定 `deepseek-flash`
   - **贝壳**：API Key + 模型名
   - **自定义**：Base URL + 模型名 + API Key 全自己填
   - 所有提供商都可开思考（开关 + 程度；DeepSeek 用 `thinking.type`，自定义可选参数风格）。模型列表里显示的名字由「模型名+思考状态」自动拼接
2. **测试配置**：勾选要对比的模型（可多选，顺序执行）；选数据集；设并发数（1=串行）、轮数（数据集重复遍数）、stream、max_tokens 等。
3. **看结果**：运行时只有一条进度；完成后依次是 总览指标（单模型为大数字卡片：TTFT 均值/p50/p95、总时延 p50/p95、tok/s、缓存命中率；多模型为对比表）→ TTFT 与 tok/s 两张波动图 → 折叠的分组统计 → 折叠的单请求明细。
4. **结果留存**：每次跑完自动存 `data/runs/bench-时间-运行ID.json`（完整明细+汇总，可直接用文本编辑器打开）；页面底部「历史测试」列出所有落盘文件，可随时查看或删除。

## 内置数据集（`datasets.py`，只读）

| 数据集 | 内容 | 用途 |
|---|---|---|
| 基础单轮 | 短（一句话）/ 中（~1k token）/ 长（~4k token）| 看输入长度对 TTFT 的影响；多轮跑同一样本可观察缓存 |
| 多轮缓存 | 8 轮旅行规划 + 6 轮技术选型的固定剧本 | 逐轮回放、前缀累积触发 prompt cache |

## 指标口径

- **TTFT**：请求发出 → 首个正文 token；思考模型先流式输出 `reasoning_content`，思考时长单独展示（首正文 − 首思考）
- **tok/s**：`usage.completion_tokens ÷ (结束 − 首个任意 token)`，token 数以 usage 为准
- **缓存命中率**：命中 tokens ÷ prompt tokens，自动识别 DeepSeek `prompt_cache_hit_tokens` 与 OpenAI `prompt_tokens_details.cached_tokens` 两种字段；**服务商不返回则显示 N/A**（注意 DeepSeek 缓存按 64 token 块存储，很短的 prompt 永远 0% 命中，属正常）
- 汇总：mean / P50 / P95 / P99 + 错误率

## 注意

- **API Key 只存在本机**：`data/` 目录已加入 `.gitignore`，其中的 `profiles.json`（含 Key）与 `runs/`（测试结果）不会进版本库，克隆后首次运行会自动创建空目录
- 流式拿 usage 依赖 `stream_options: {"include_usage": true}`（默认开启）；个别服务端不认这个字段时可以关掉
- 思考关了就不需要选程度；思考开着时，max_tokens 会被思考消耗，注意给足
- 运行历史保存在内存（最近 20 次），重启清空；完整结果以文件形式留在 `data/runs/`，可在页面底部查看
- 本地 mock（可选，无 Key 体验）：`.venv/bin/python mock_server.py` 后添加「自定义」模型，Base URL 填 `http://127.0.0.1:8766/v1`
