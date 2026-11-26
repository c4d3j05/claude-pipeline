## ADDED Requirements

### Requirement: Dashboard actions
With `DASHBOARD_ACTIONS` on, the dashboard SHALL offer: run explorer now, answer an explorer escalation
question, send, hold or unhold a batch, retry a blocked ticket, and restart a service. Each action SHALL ask
for confirmation, show its outcome, and be logged with its time and origin.

#### Scenario: Recheck after answering
- **WHEN** the owner answers an escalation A and presses Run now
- **THEN** explorer gets a run request and the outcome reads "run requested"

### Requirement: Unsafe actions refused
A restart SHALL be refused with the reason while the service is busy: explorer with an active run, the
dispatcher with tickets building, the planner with an investigation working. Timers are never restarted.

#### Scenario: Explorer busy
- **WHEN** explorer is running B-3 rechecks and Restart explorer-serve is pressed
- **THEN** nothing restarts and the dashboard says why
