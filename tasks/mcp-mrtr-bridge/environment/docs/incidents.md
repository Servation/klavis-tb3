# Open incidents since the 2026-07-28 rollout

## INC-2291: duplicate tickets after a network change (high)

Ridgeline Health: "The assistant asked me to confirm a support ticket. I
confirmed, and my laptop switched from Wi-Fi to the dock's ethernet. The
assistant said the confirmation failed ('Invalid requestState: unknown or
expired') and started over, I confirmed again, and now there are two identical
tickets. Third time this week."

Client logs: the stream of the confirming request dropped, and the client
re-sent the same request on a new connection, as the 2026-07-28 transport
tells it to.

## INC-2304: approvals fail about half the time (high)

Corvid Analytics: approving a deployment fails roughly every other time with
`-32602 Invalid requestState`. Retrying the approval never helps; starting the
whole action again sometimes does. Began the day we moved the gateway to two
instances.

## INC-2310: progress from another import (medium)

Lumen Retail: "I started two contact imports in two browser tabs. The progress
bar in the second tab shows the first import's rows, and sometimes the second
import fails straight away with 'Request id 1 is already in use in this
session'." Both tabs run the same web client, signed in as the same user.

## INC-2325: approvals left running after a deploy (high)

Corvid Analytics: during Tuesday's rolling deploy (each gateway instance is
stopped and started again in turn), approvals sent after the deploy failed
with `Invalid requestState`, and the deployments they belonged to stayed
"in progress" upstream for hours until the deploys team cancelled them by
hand.

## INC-2331: imports ask twice and run twice (medium)

Lumen Retail: since the imports team turned on connection recycling on their
server, imports sometimes ask for the e-mail column a second time, and the
contacts end up imported twice.

## INC-2318: stopped sync ran anyway (medium)

Lumen Retail: "I pressed Stop on a contact sync a few seconds after starting
it. The client showed it as cancelled, but the sync still ran to the end and
overwrote our contacts."
