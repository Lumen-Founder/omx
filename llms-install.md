# Installing OMX (for AI agents)

OMX is an MCP server (stdio) for Hyperliquid order execution. The PyPI package is
`omx-hyperliquid`; the server command is `omx-hyperliquid`.

## 1. Install in demo mode (always do this first)

Demo mode needs no key, account, config or network and cannot trade. Add this server to the
MCP client's configuration:

```json
{
  "mcpServers": {
    "omx": {
      "command": "uvx",
      "args": ["omx-hyperliquid"]
    }
  }
}
```

If `uvx` is not available, install uv (Windows: `powershell -ExecutionPolicy ByPass -c "irm
https://astral.sh/uv/install.ps1 | iex"`; macOS/Linux: `curl -LsSf https://astral.sh/uv/install.sh |
sh`) and open a new terminal, or run `pip install omx-hyperliquid` and use `"command":
"omx-hyperliquid"` with `"args": []`.

Check: call `get_quote` with `{"coin": "ETH"}`. The answer has `"ok": true` and `"demo": true`.
Then `preview_order` and `submit_order` with `{"coin": "ETH", "side": "buy", "size": 0.1, "algo":
"twap", "duration_s": 10, "slices": 5}` and poll `order_status` until `"state": "done"`.

## 1b. Paper trading on live prices (still no key)

`"args": ["omx-hyperliquid", "--paper"]` gives the same tools with simulated fills against
Hyperliquid mainnet's live public order books (public market data only; nothing is signed or sent).
Orders must be at least 10 USD of notional, as on Hyperliquid. Answers carry `"paper": true`.

## 2. Real trading (only when the user asks, and only by the user)

Do not create wallets, handle private keys or approve fees for the user. Tell the user:

1. OMX is not available to persons in the United States, Ontario (Canada) or sanctioned
   territories (Hyperliquid Terms of Use). There is no way around this.
2. Start on Hyperliquid testnet. Create an API (agent) wallet in your own account; it can trade but
   cannot withdraw. Save its key in a file only you can read.
3. Run `omx init --account <your main address> --key-path <agent key file> --enable-agent` (add
   `--network mainnet` later). It asks the eligibility questions.
4. Optional fee: OMX adds a 1 bp Hyperliquid builder fee only after you approve it from your own
   wallet with `omx approve-builder`; `omx init --no-builder` sends none.
5. Run `omx daemon` in its own terminal and keep it open. `omx doctor` checks the setup.
6. In the MCP client, set the server's environment `OMX_CONFIG` to the config path (or add
   `"--config", "<path>"` to `args`).

Full guide: https://github.com/Lumen-Founder/omx-hyperliquid-mcp#quick-start-testnet
