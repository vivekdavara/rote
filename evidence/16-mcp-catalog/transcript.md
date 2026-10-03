# MCP transcript (an agent's view of the catalog)

`rote mcp serve --tenant harbor`, driven by the official MCP Python client.

## tools/list

### `coreone__member__get_savings_balance`

Look up a member by member number and return the current Share Savings balance. Read-only. Business outcomes to handle (status=business_outcome, not an error): MEMBER_NOT_FOUND (No member has this member number.); ACCESS_RESTRICTED (The member record is restricted (for example, an employee account).). Runs a reviewed, approved recording deterministically; no model is involved.

annotations: `{"title": "Look up a member by member number and return the current Share Savings balance.", "read_only_hint": true, "destructive_hint": false, "idempotent_hint": true}`

```json
{
  "properties": {
    "member_id": {
      "type": "string",
      "description": "Six-digit member number.",
      "pattern": "^[0-9]{6}$"
    }
  },
  "required": [
    "member_id"
  ],
  "type": "object",
  "additionalProperties": false
}
```

### `coreone__member__open_sub_account`

Open a new share sub-account for a member, funded from one of their accounts. IRREVERSIBLE: call with mode=preview first and show the returned review values to the member; then call with mode=commit, the commit_token, and a fresh idempotency_key. Business outcomes to handle (status=business_outcome, not an error): VALIDATION_REJECTED (CoreOne rejected the request; the app's message says why.). Runs a reviewed, approved recording deterministically; no model is involved.

annotations: `{"title": "Open a new share sub-account for a member, funded from one of their accounts.", "read_only_hint": false, "destructive_hint": true, "idempotent_hint": false}`

```json
{
  "properties": {
    "member_id": {
      "type": "string",
      "description": "Six-digit member number.",
      "pattern": "^[0-9]{6}$"
    },
    "share_type": {
      "type": "string",
      "enum": [
        "Regular Savings",
        "Holiday Club",
        "Money Market"
      ]
    },
    "nickname": {
      "type": "string",
      "pattern": "^[A-Za-z0-9 ]{1,20}$"
    },
    "deposit": {
      "type": "string",
      "description": "Initial deposit in dollars.",
      "pattern": "^-?\\d+(\\.\\d+)?$"
    },
    "funding_suffix": {
      "type": "string",
      "description": "Suffix of the member's account that funds the deposit.",
      "pattern": "^S[0-9]{2}$"
    },
    "mode": {
      "type": "string",
      "enum": [
        "preview",
        "commit"
      ],
      "default": "preview",
      "description": "preview first; commit only with the token the preview returned"
    },
    "commit_token": {
      "type": "string",
      "description": "From a preview of exactly these inputs."
    },
    "idempotency_key": {
      "type": "string",
      "description": "Unique per intended commit; a repeat returns the result."
    }
  },
  "required": [
    "member_id",
    "share_type",
    "nickname",
    "deposit",
    "funding_suffix"
  ],
  "type": "object",
  "additionalProperties": false
}
```

## tools/call `coreone__member__get_savings_balance`

arguments (inputs redacted below):

`is_error=False`

```json
{
  "status": "succeeded",
  "outputs": {
    "savings_balance": {
      "amount": "[redacted]",
      "currency": "USD"
    }
  },
  "commit_state": "none"
}
```

## tools/call `coreone__member__get_savings_balance`

arguments (inputs redacted below):

`is_error=False`

```json
{
  "status": "business_outcome",
  "outcome": {
    "code": "MEMBER_NOT_FOUND",
    "message": "No member has this member number.",
    "step_id": "open_search",
    "messages": [
      "No members found matching the search criteria."
    ]
  },
  "commit_state": "none"
}
```

## tools/call `coreone__member__open_sub_account`

arguments (inputs redacted below):

`is_error=False`

```json
{
  "status": "preview",
  "outputs": {
    "review_share_type": "Money Market",
    "review_deposit": {
      "amount": "40.00",
      "currency": "USD"
    },
    "review_funding_account": "S10 - Everyday Checking"
  },
  "preview": {
    "values": {
      "review_share_type": "Money Market",
      "review_deposit": {
        "amount": "40.00",
        "currency": "USD"
      },
      "review_funding_account": "S10 - Everyday Checking"
    },
    "commit_token": "<signed token, elided>",
    "expires_at": "2026-10-03T14:44:01Z"
  },
  "commit_state": "none"
}
```
