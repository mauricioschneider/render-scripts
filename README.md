# How to Use

## `copy_workspace.py`

```
RENDER_SRC_API_KEY=[source_api_key] RENDER_DST_API_KEY=[dest_api_key] python copy_workspace.py --src-owner-id tea-[...] --dst-owner-id tea-[...] [--overwrite-env-vars] [--execute]
```

Without `execute`, it will run in dry mode, listing all the items that would be copied during an actual execution.
