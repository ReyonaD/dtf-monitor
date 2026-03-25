# Project Brief — DTF Floor Monitor

## Core Purpose
Real-time monitoring system for a DTF (Direct to Film) printing warehouse with 5 printing machines, each on its own Windows PC. Provides full visibility into what's happening on the warehouse floor.

## Problem Statement
Owner has zero visibility into warehouse operations. Orders come from 5-6 Shopify stores, designers upload files to Dropbox, operators download and print — but there's no way to see which files are on which machine, what's printing, or how much work is queued.

## Core Requirements
1. **PC Agent** — Background program on each Windows PC that watches the folder where print files are stored
2. **Central Server** — Collects information from all 5 PCs in one place
3. **Live Dashboard** — Browser-based view showing everything across all machines at once

## Key Features
- See each machine: which files are on it, what's printing, what's queued
- Total queued inches per machine (calculated dynamically from image DPI metadata)
- Operators tap "Print" button to mark a file as printing; clicking another file auto-completes the previous
- Search bar on dashboard to find which PC has a specific file
- History log of completed jobs
- Daily stats: total inches printed per machine

## File Types
- PNG and TIFF images
- DPI is read from each file's metadata (not fixed — varies per file)
- Print inches = image height in pixels / DPI

## Target Users
- **Owner (Alp)** — Views the dashboard for full floor visibility
- **Operators** — Use the agent GUI on each printer PC to mark print status
