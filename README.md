# How to build and run Jira MCP Service
```bash
docker build -t skypilot-jira .
docker run --rm -p 8009:8009 --env-file .env.local --network skypilot --name skypilot-jira skypilot-jira
```