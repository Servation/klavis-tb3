# Bridge contract: `tools/call` through the gateway

This contract defines what `tools/call` guarantees to 2026-07-28 clients when
the tool is served by a 2025-11-25 upstream server. The MCP specifications in
`docs/spec/` define the protocols on either side; where they leave the
gateway's behaviour open, this document decides it.

## Terms

- **Call**: what a client starts with a `tools/call` request that carries no
  `requestState`. Every such request starts a new call. (Under 2026-07-28 a
  client whose initial request stream broke re-issues it as a new request,
  which is a new call.)
- **Execution**: the upstream `tools/call` the gateway runs on the legacy
  server for a call.
- **Round k**: the k-th `InputRequiredResult` the gateway returns for a call
  carries `state_k`. A *retry for round k* is a `tools/call` carrying
  `state_k` and the `inputResponses` for round k's input requests.
- **Outcome**: what answers a request. The outcome of the initial request is
  round 1's `InputRequiredResult` or the call's final result; the outcome of a
  retry for round k is round k+1's `InputRequiredResult` or the final result.
- **Attached request**: of the call's requests whose response stream is open
  and not yet answered, the one the gateway received most recently. A call has
  at most one attached request.
- **Home instance**: the instance that received the call's initial request.

Legacy servers may close the connection that carries a call's stream at any
time, as 2025-11-25 allows: they send a `retry` field first, and the stream
is resumed with `GET` and `Last-Event-ID`. That is neither a cancellation nor
the end of the call.

## Guarantees

**G1 Single execution.** A call starts at most one execution, and no request
carrying a `requestState` starts another. Calls never share an execution or
each other's outcomes, whatever JSON-RPC ids or progress tokens their clients
chose.

**G2 Round idempotency.** A retry for round k receives the outcome of round k,
on either instance and however many times it is re-sent, including when the
execution produced that outcome while no request was attached. The only
exception is a request that stops being the attached request because a newer
request of the same call arrived: it may end without a response. Round k's
`inputResponses` reach the legacy server exactly once, taken from the first
retry for round k that the gateway accepted.

**G3 Binding.** A `requestState` is rejected with JSON-RPC error `-32602`, with
no effect on any execution, when it is presented by a principal other than the
one it was issued to, on a `tools/call` for another tool or with other
arguments, after it was altered, or after it expired (`GATEWAY_STATE_TTL`
seconds after it was issued).

**G4 Cancellation.** The gateway sends the legacy server
`notifications/cancelled` for an execution exactly when no client can still
obtain the call's outcome:

- (a) the initial request's stream closed before the gateway sent its
  response: within 2 s of the close;
- (b) the call's latest `requestState` expired while no request was attached:
  within 3 s of the expiry.

Apart from G6, nothing else cancels an execution. In particular, closing the
stream of a retry, or of a request that already received its response, is not
a cancellation, and an execution that finishes while no request is attached
keeps its outcome until the call's latest `requestState` expires.

**G5 Progress.** Progress notifications the legacy server sends for an
execution are forwarded to the call's attached request, and only to it, as
`notifications/progress` carrying that request's own `progressToken` (none at
all if that request sent no `progressToken`), in non-decreasing order, and
never after that request's response.

**G6 Instance loss.** An instance can die at any moment (it is killed, or its
host fails) and may later be restarted with the same configuration and an
empty memory. Every execution of a call whose home instance died is then
cancelled upstream within 10 s of the death, whether or not the instance comes
back, and is never executed again. Requests for such a call are rejected with
`-32602`, or answered with the outcome of their round if it is still
available. Calls with another home instance are unaffected.
