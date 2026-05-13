# Project Memory

## Execution Defaults

- This machine is dedicated for agent work. Prefer unrestricted/local execution for project tasks. If a sandbox blocks local sockets, browser/CDP, network, or filesystem behavior needed by the task, do not work around it with a different workflow; run the intended workflow outside the sandbox when the tool mode allows it.
- All Python commands for this project should use:
  `/home/zhiping/research-env/bin/python`
- Do not assume system `python3` has the right dependencies.

## Browser Workflow

- When a task needs a headed browser, follow the headed-browser path implemented in `complete_paper_extraction.py` around lines 934-977:
  - check whether Chrome CDP is ready on `127.0.0.1:9222`;
  - if it is not ready, start `chrome_launcher.py` with the same Python executable;
  - connect with `p.chromium.connect_over_cdp("http://localhost:9222")`;
  - reuse the existing context when present, otherwise create a new context with `accept_downloads=True`;
  - create a new page from that context.
- For APS papers, use the forced headed path. Run extraction as:
  `/home/zhiping/research-env/bin/python complete_paper_extraction.py --doi <DOI> --force-headed`
- Do not validate APS access with `requests` or headless browser paths; APS must be validated through the headed workflow above.

## Repository Workflow

- After code changes, commit and push to:
  `https://github.com/zhiping0913/Download_paper`
- Preserve unrelated dirty worktree changes. In particular, do not restore or commit unrelated deletions/modifications unless explicitly requested.
