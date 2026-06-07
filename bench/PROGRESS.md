# Batch progress — paused at 2026-06-03

## Done (25/42 DevBench wave)

| Model | Tier | S0 | S1 | S2 | S3 | S4 | S5 | Overall |
|-------|------|----|----|----|----|----|----|---------|
| GPT-5.5 | frontier | 100% | 100% | 100% | 100% | 100% | 100% | **100%** |
| Claude Opus 4.7 | frontier | 100% | 100% | 100% | 100% | 100% | 100% | **100%** |
| DeepSeek V4 Pro | frontier | 100% | 100% | 100% | 100% | 66.7% | 100% | **100%** |
| Llama 4 Maverick | mid_tier | 0% | 0% | 0% | 100% | 0% | 0% | **0%** |
| Claude Sonnet 4.6 | mid_tier | 100% | 100% | 100% | 100% | — | — | **100%** (4/6) |
| GPT-5.4 Mini | compact | 100% | — | — | — | — | — | **100%** (1/6) |
| GPT-5.4 Nano | compact | — | — | — | — | — | — | pending |

## Pending

### DevBench wave (continue): 17/42 remaining
- Claude Sonnet 4.6: S4, S5
- GPT-5.4 Mini: S1, S2, S3, S4, S5
- GPT-5.4 Nano: all 6 tasks

### Additional OR wave (not started): 36 combos
Claude Opus 4.8, GPT-5.5 Pro, GPT-5.4, Gemini 3.1 Pro, DeepSeek V4 Flash, Llama 4 Scout

### DW wave (not started): 24 combos
Kimi K2.6, GLM 5.1, DeepSeek V4 Pro (DW), Qwen3.5 397B

### Local wave (not started): 12 combos
gemma3 (Ollama), Apple FM

## Resume command
```
PYTHONPATH=. python3 bench/batch.py --wave devbench --trials 3
```
