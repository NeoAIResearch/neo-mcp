# Neo MCP Example Prompts

Use these from the **Prompts** tab in Postman. Path args are **optional** (workspace-relative, e.g. `data/fraud.csv`). Omit them to use sensible defaults.

## ML and classification

| Prompt | Optional args | Use for |
|--------|---------------|---------|
| `train-model` | `path`, `goal` | Tabular ML (classification, regression, fraud) |
| `fine-tune-classifier` | `path` | Text classification with cross-validation |

## LLM and GenAI

| Prompt | Optional args | Use for |
|--------|---------------|---------|
| `fine-tune-llm` | `base_model`, `path`, `goal` | Fine-tune Llama/Qwen/Gemma |
| `build-rag-pipeline` | `path`, `goal` | RAG: ingest, chunk, embed, retrieve |
| `build-ai-agent` | `description` (required) | Multi-step agent with tools |
| `benchmark-prompts` | `path` | LLM prompt evaluation |

## Pipelines and debugging

| Prompt | Optional args | Use for |
|--------|---------------|---------|
| `build-ml-pipeline` | `description` (required) | End-to-end ML/ETL pipeline |
| `fix-training-run` | `context` (required) | Debug failed training |

## Data and vision

| Prompt | Optional args | Use for |
|--------|---------------|---------|
| `run-eda` | `path`, `goal` | Exploratory data analysis |
| `train-vision-model` | `path`, `goal` | Image classification or detection |

## Workflow

1. Run a prompt (optional: fill `path`) → copy generated message
2. **Tools** → `neo_submit_task` with `message` + `workspace` = `NEO_WORKSPACE_DIR`
3. **Tools** → `neo_task_status` → `neo_get_messages` when COMPLETED
