# Product Context

## Why This Project Exists
The warehouse owner runs 5 DTF printing machines processing orders from multiple Shopify stores. Currently there is no way to know what's happening on the floor without physically walking to each machine. This system provides a single screen showing all machines, their queues, and print status in real time.

## Problems It Solves
- No visibility into which files are on which PC
- No way to know which machine is busy vs idle
- No tracking of how many inches are queued or printed per day
- No history of completed jobs or productivity metrics
- Finding a specific file requires checking each PC manually

## How It Should Work
1. Operators download print files to a specific folder on each PC
2. The agent automatically detects new files and reports them to the server
3. The dashboard updates in real time showing all machines
4. Operator clicks "Print" next to a file when they start printing it
5. Clicking "Print" on the next file auto-marks the previous as completed
6. Owner sees everything on the dashboard from any browser

## User Experience Goals
- **Dashboard**: Warehouse floor monitor feel — functional, clear, readable at a glance from across a room
- **Agent**: Minimal operator interaction — just one button per file, no complexity
- **Setup**: First-run wizard asks operator to pick their folder and name their machine — that's it
