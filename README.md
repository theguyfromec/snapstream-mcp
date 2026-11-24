# snapstream-mcp

MCP server for the SnapStream tenant API (the cloud API served through API Gateway).

## Status

Scaffold only. The server code is not written yet.

## Credentials

Copy `secrets.yaml.example` to `secrets.yaml` and fill it in from Cloud Admin (instance > Modify > API, shown after Provision API). `secrets.yaml` is gitignored. Never commit it, and never send these values through chat, Slack or email.

Each call gets an access token from the Cognito token endpoint using the client ID and secret, then sends `Authorization: Bearer <token>` and `x-api-key`. Requests take the form `POST {base_url}/{Service}/json/{Operation}` with a JSON body keyed by parameter name.

## Safety

Development runs against the demo instance only. Read operations come first. Gateway rate limits do not cap the load that operations put on the recorder, so write operations need their own throttling and an explicit approval step.
