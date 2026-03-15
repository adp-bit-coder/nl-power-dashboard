Start the power dashboard server in the background, then confirm it is running. No explanation needed — just do it.
Trigger: `/power`

Run this command in the background (non-blocking):

```bash
python skills/power/server.py
```

The server will automatically open http://127.0.0.1:5050 in the browser after 1 second.
If it is already running, it will just open the browser tab.

After starting the server, respond with a single short confirmation, for example:
"Dashboard open at http://127.0.0.1:5050"
