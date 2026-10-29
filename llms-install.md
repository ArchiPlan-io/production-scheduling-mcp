# Install in Cline

1. Install Python and [uv](https://docs.astral.sh/uv/).
2. Open Cline MCP settings and add this server:

```json
{
  "mcpServers": {
    "production-scheduling": {
      "command": "uvx",
      "args": ["production-scheduling-mcp"],
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

Cline will start the server over stdio. Ask it to schedule orders from your machine list, due dates, step durations, and eligible machines.
