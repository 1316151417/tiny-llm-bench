# tiny-llm-bench

对 OpenAI 兼容的 `/v1/chat/completions` 接口做压测，主要看两个指标：**TTFT（首 token 延迟）** 和 **输出速度（tok/s）**，支持多模型对比。

## 启动

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync        # 安装依赖
uv run app.py  # 启动，打开 http://127.0.0.1:8765
```

## 用法

1. **添加模型**：选提供商（DeepSeek / 智谱 Coding Plan / 贝壳 / 自定义），填 API Key；内置的预设会带好 base_url，模型从下拉里选
2. **测试配置**：勾选要对比的模型，选数据集，设并发数（1 = 串行）和轮数
3. **看结果**：多模型对比表 + TTFT / tok/s 波动图；跑完自动存到 `data/runs/`，可在页面底部回看

## 指标

| 指标 | 含义 |
|---|---|
| **TTFT** | 请求发出 → 收到第一个正文 token 的耗时，第一次响应快不快看它 |
| **tok/s** | 输出 token 数 ÷ 生成耗时，模型吐字快不快看它 |
| 缓存命中率 | 命中 tokens ÷ prompt tokens，服务商返回该字段时才有 |

开思考的模型会先输出一段思考内容，TTFT 会被思考时间污染，所以思考时长单独统计。流式（stream）是测 TTFT 的前提，关掉只能拿到总耗时。

选模型时表单会跟着变：

- **思考档位**（`reasoning_effort`）不是所有模型都有。GLM-5.3 / 5.3-Flash 只有 low/high/max，GLM-4.6 及更早只有二元开关，DeepSeek 支持 low/high/max（官方把 medium 映射为 high）。没有档位的模型不会显示「思考程度」
- **GLM-5.3 / 5.3-Flash 强制思考**，官方不接受关闭（传了报错），这两个模型的开关会自动锁定为开启
- 档位只在**厂商自家端点**（DeepSeek、智谱）下发；网关（贝壳/自定义）的转发语义未确认，页面上会说明并只发开关

## 数据集

- **基础单轮**：短 / 中（~1k token）/ 长（~4k token），看输入长度对 TTFT 的影响
- **多轮缓存**：固定剧本逐轮回放，前缀累积触发 prompt cache，看缓存命中率随轮次的变化

## 注意

API Key 只保存在本机 `data/profiles.json`，`data/` 已加入 `.gitignore`，不会进版本库。
