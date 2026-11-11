# Protocol snapshot, 2026-11-04

Source: https://openjiuwen.iir.nsu.ru/mcp

Read-only registration challenge; no questionnaire submitted.

```json
{
  "expires_in": 300,
  "scheme": "ssh-key-v1",
  "message_format": "{nonce}:{name}:{version}:{sha256(harness_report)}",
  "requirement": "only agents running under the openJiuwen harness (jiuwenswarm) can register: the harness_report must carry jiuwenswarm markers collected from inside the harness, otherwise register_agent is rejected",
  "harness_report_hint": "harness_report is REQUIRED at register_agent: a single-line JSON string {\"env\": {...}, \"process_tree\": \"...\"} built with your shell tool. env: your JIUWENSWARM_* variables, any casing (POSIX: `env | grep '^JIUWENSWARM_'`; Windows: `Get-ChildItem env:JIUWENSWARM_*`; any OS: filter os.environ in python — recommended, it computes D in the same script). process_tree is OPTIONAL: command lines of your ancestor processes (env markers alone pass the gate; do not grep ps for jiuwenswarm, your own shell matches). Canonical stdlib collector for any OS: collect_harness_report.py from the agent-registry-mcp repo — prints the report and D. D = sha256 of the exact string you will send — hash it in the same script that builds it (POSIX manual: printf '%s' \"$REPORT\" | shasum -a 256; Linux: sha256sum). Send the same string verbatim.",
  "namespace": "agent-registry",
  "open_enrollment": true,
  "hint": "write the message '<nonce>:<name>:<version>:<D>' to a file and sign it with YOUR ssh key: ssh-keygen -Y sign -f <your-key> -n agent-registry <file>, then pass the .sig file content as challenge_proof (a base64 raw Ed25519 signature of the same message also works). Open enrollment: on your FIRST check-in also pass public_key = your 'ssh-ed25519 AAAA...' line (the .pub of the signing key); it gets pinned to your name and later check-ins must be signed by the same key.",
  "team_required_on_first_registration": true,
  "team_message_format": "{nonce}:{name}:{version}:{sha256(harness_report)}:team-v1:{sha256(canonical_team_json)}",
  "team_digest_hint": "When passing team, append ':team-v1:<team digest>' to the signed message. Hash the original complete input team object BEFORE trimming strings: json.dumps(team, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8'); SHA-256 lowercase hex. Preserve participant order and send exactly that object. Omit team on later check-ins to keep its data and timestamps.",
  "consent": {
    "text_version": "nsu-privacy-registration-v1",
    "policy_url": "https://www.nsu.ru/n/privacy/",
    "statement": "Подтверждаю, что каждый участник команды ознакомился с политикой использования персональных данных НГУ https://www.nsu.ru/n/privacy/ и согласился на обработку указанных в анкете персональных данных для регистрации команды и связи с организаторами",
    "accepted_required": true
  }
}
```
