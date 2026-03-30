# How to Use

## `copy_workspace.py`

```
RENDER_SRC_API_KEY=[source_api_key] RENDER_DST_API_KEY=[dest_api_key] python copy_workspace.py \
  --src-owner-id tea-[...] \
  --dst-owner-id tea-[...] \
  [--project "My Project"] \
  [--overwrite-env-vars] \
  [--execute]
```

Without `--execute`, it runs in dry-run mode, listing all items that would be copied.

### Options

| Flag | Description |
|------|-------------|
| `--src-owner-id` | Owner ID (`tea-`/`usr-`) of the source workspace |
| `--dst-owner-id` | Owner ID (`tea-`/`usr-`) of the destination workspace |
| `--project` | Only copy resources belonging to this project (by name). Omit to copy all projects. |
| `--overwrite-env-vars` | For services that already exist in the destination, overwrite their env vars |
| `--execute` | Perform the actual copy (default is dry-run) |
| `--src-api-key` | Source API key (prefer `RENDER_SRC_API_KEY` env var) |
| `--dst-api-key` | Destination API key (prefer `RENDER_DST_API_KEY` env var) |
