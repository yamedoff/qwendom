# Frontend browser delivery

Version: 1

## Purpose

Deliver a bounded frontend artifact and browser-render evidence inside the
assigned AgentBay environment. This skill belongs only to the Frontend
Engineer specialist.

## Required execution lifecycle

1. Work only on assigned artifact paths and preserve unrelated files.
2. Write the renderable site to `/workspace/app/dist/index.html`. Create parent
   directories through the supported file tools before writing the entry file.
3. List and read back `/workspace/app/dist` and `index.html` to confirm the
   exact entry path and contents before execution.
4. Do not use `run_code` for HTML or product-file imports. Use
   `execute_command` only for supported deterministic checks that the granted
   environment can run; report a rejected or unavailable check truthfully.
5. Call `browser_render` exactly once, only after the required entry file
   exists. Treat its desktop/mobile screenshot references, console evidence,
   and render manifest as output evidence.
6. Export required owned artifacts only after they exist and have been
   confirmed. Never claim an export, render, image, or video attempt that did
   not occur.
7. Do not attempt image or video generation unless those tools are granted. If
   media is requested but unavailable, record it as a truthful blocker/report.
8. Close the execution environment after completing or blocking the work.

## Output contract

Return changed and read-back paths, deterministic checks and their outcomes,
browser screenshot/console references, exported artifacts, unavailable media
blockers, and cleanup status. Browser evidence is not a substitute for an
independent Test Engineer validation verdict.
