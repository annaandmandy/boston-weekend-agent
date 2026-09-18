# Security

Never commit API keys, AWS credentials, `.env` files, EC2 private keys, or exported
Lambda environment values to this repository.

Production secrets are expected in AWS Secrets Manager:

- `boston-weekend-agent/events-api`: `TICKETMASTER_API_KEY`
- `boston-weekend-agent/openai`: `OPENAI_API_KEY`

Lambda environment variables contain only the corresponding secret ARN. Each
execution role should have `secretsmanager:GetSecretValue` access to only the
secret used by that function.

The public event-feedback API must not accept names, email addresses, social
handles, or arbitrary event metadata from the browser. It validates event IDs
against the current public report, hashes the random browser-local visitor ID,
uses DynamoDB conditional transactions to enforce one active vote, restricts
CORS to the production site and local development origins, and applies API
Gateway throttling. Anonymous browser IDs are a convenience-level duplicate
control, not proof of a unique person.

If a credential was ever committed or hard-coded, remove it from the code and
rotate it at the provider. Deleting it from the latest Git commit is not enough.
