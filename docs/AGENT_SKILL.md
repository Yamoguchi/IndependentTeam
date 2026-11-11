---
name: challenge-registration
description: Register a real TechArena openJiuwen team from a local JSON using signed MCP challenges.
---

Work only in /Users/mustafaevrodion/Projects/openjiuwen. Use your real bash tool.
Read README.md. Never fabricate questionnaire fields, consent, reports or harness markers.
Do not read .env or private key contents into the conversation. Never send the API key to the registry.
Use `uv run openjiuwen-challenge discover` to check the MCP server.
Run `uv run openjiuwen-challenge policy` and show the policy URL and full statement
to the human sender. Obtain explicit confirmation that EVERY participant has read
the policy and consented. Ask the human to set accepted=true in their team.json.
The user must explicitly authorize sending that questionnaire and the real harness
report to https://openjiuwen.iir.nsu.ru/mcp. Do not submit examples.

Validate the JSON, generate the local key once (do not replace an existing key),
then run the client's `run --name <human-chosen-unique-name> --team team.json
--consent-sha256 <hash-from-policy>` command through your bash tool.
Use --once only for an explicitly requested short session; it does not guarantee qualification acceptance.
Keep the normal client alive while the agent is working and stop it at session end. For ongoing work use the bash tool native run_in_background option (not nohup or shell &) with a
PID file and .state/session.log; heartbeat and shutdown are managed in that same process.
Do not launch a second check-in while one is active. A subsequent check-in rotates the ID.
On shutdown send SIGTERM to that exact client's PID, and verify its deregistered event.
Do not manually call register_agent in parallel with this client.
Never use organizer-only read APIs to recover an ID.

Success requires attested=true, agent.proof.verified=true, the expected key
fingerprint, a valid own registration_id, and all team/consent timestamps.
Do not describe local tests or discovery as a completed contest registration.
The team questionnaire remains with organizers after deregister_agent.
