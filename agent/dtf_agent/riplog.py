"""
FlexiPrint RIPLOG.HTML parser + watcher — moved VERBATIM from the legacy agent.py
(RIPLogParser / RIPLogWatcher / parse_copies). The server's floor dashboard is fed
from build_file_list() via the heartbeat; do not change its output shape.
"""
import os
import re
import time
import threading
from datetime import datetime, timedelta

# ── RIPLOG Parser ──

class RIPLogParser:
    """Parses FlexiPrint RIPLOG.HTML to extract RIP jobs and nest info."""

    # Regex to strip &nbsp; sequences
    _NBSP = re.compile(r'&nbsp;')
    # Regex to strip HTML tags
    _TAGS = re.compile(r'<[^>]+>')

    @staticmethod
    def _clean(text):
        """Remove HTML tags and &nbsp; from a value string."""
        text = RIPLogParser._NBSP.sub(' ', text)
        text = RIPLogParser._TAGS.sub('', text)
        return text.strip()

    @staticmethod
    def parse_file(filepath):
        """
        Parse RIPLOG.HTML and return all blocks in order.
        Each entry is a dict with a 'block_type' field: 'rip' or 'print'.

        "Start RIP Job" = RIP processing only (has full file path).
        "Start Printing" = actual file output (has filename or "Nest (x jobs)").
        """
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading RIPLOG: {e}")
            return []

        blocks = re.split(r'<BR><BR>', content)
        entries = []

        for block in blocks:
            if 'Start RIP Job' in block:
                job = RIPLogParser._parse_block(block, is_rip=True)
                if job:
                    job['block_type'] = 'rip'
                    entries.append(job)
            elif 'Start Printing' in block:
                job = RIPLogParser._parse_block(block, is_rip=False)
                if job:
                    job['block_type'] = 'print'
                    entries.append(job)

        return entries

    @staticmethod
    def _parse_block(block, is_rip=False):
        """Parse a single RIP Job or Printing table block into a dict."""
        job = {}

        # Extract all TH -> TD pairs
        pairs = re.findall(
            r'<TH[^>]*>(.*?)</TH>\s*<TD[^>]*>(.*?)</TD>',
            block,
            re.DOTALL | re.IGNORECASE
        )

        for th_raw, td_raw in pairs:
            key = RIPLogParser._clean(th_raw).rstrip(':')
            val = RIPLogParser._clean(td_raw)
            if not key:
                continue

            if key == 'File':
                job['file'] = val
            elif key == 'Device name':
                job['device'] = val
            elif key == 'File Size':
                job['file_size'] = val
            elif key == 'Sender':
                job['sender'] = val
            elif key == 'Job Type':
                job['job_type'] = val
            elif key == 'Dimensions':
                job['dimensions'] = val
            elif key == 'Resolution':
                job['resolution'] = val
            elif key == 'RIP Start Date and Time':
                job['rip_start'] = val
            elif key == 'RIP End Date and Time':
                job['rip_end'] = val
            elif key == 'RIP Duration':
                job['rip_duration'] = val
            elif key == 'Output Start Date And Time':
                job['output_start'] = val
            elif key == 'Output End Date And Time':
                job['output_end'] = val
            elif key == 'Info':
                if 'Job successfully done' in val:
                    job['success'] = True

        if not job.get('file'):
            return None

        # Parse nest info — file field looks like "Nest (2 jobs)"
        nest_match = re.match(r'^Nest\s*\((\d+)\s*jobs?\)', job['file'])
        if nest_match:
            job['is_nest'] = True
            job['nest_count'] = int(nest_match.group(1))
            job['file'] = f"Nest ({job['nest_count']} jobs)"
        else:
            job['is_nest'] = False
            job['nest_count'] = 0

        # Parse dimensions -> width_in, height_in
        dim_match = re.match(r'([\d.]+)\s*x\s*([\d.]+)\s*in', job.get('dimensions', ''))
        if dim_match:
            job['width_in'] = float(dim_match.group(1))
            job['height_in'] = float(dim_match.group(2))

        # Parse resolution
        res_match = re.match(r'([\d.]+)\s*x?\s*([\d.]+)', job.get('resolution', ''))
        if res_match:
            job['res_x'] = float(res_match.group(1))
            job['res_y'] = float(res_match.group(2))

        return job

    @staticmethod
    def _parse_timestamp(ts_str):
        """Parse RIPLOG timestamp like '5:20:35 PM 9/24/2025' into datetime."""
        if not ts_str:
            return None
        formats = [
            "%I:%M:%S %p %m/%d/%Y",   # 5:20:35 PM 9/24/2025
            "%m/%d/%Y %I:%M %p",       # 9/25/2025 4:18 AM
        ]
        for fmt in formats:
            try:
                return datetime.strptime(ts_str.strip(), fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def filter_recent(jobs):
        """Filter jobs to only those from today (since midnight 00:00)."""
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        recent = []
        for job in jobs:
            # Try output_end, output_start, rip_end, rip_start
            ts = (RIPLogParser._parse_timestamp(job.get('output_end'))
                  or RIPLogParser._parse_timestamp(job.get('output_start'))
                  or RIPLogParser._parse_timestamp(job.get('rip_end'))
                  or RIPLogParser._parse_timestamp(job.get('rip_start')))
            if ts and ts >= cutoff:
                recent.append(job)
            elif not ts:
                # If we can't parse the timestamp, include it (safe fallback)
                recent.append(job)
        return recent

    @staticmethod
    def build_file_list(entries):
        """
        Convert parsed RIPLOG entries into file info dicts.
        Only "Start Printing" entries produce files.
        For nests, look back at preceding "Start RIP Job" entries for filenames.
        Only includes entries from today (resets at midnight).

        Returns list of dicts with: filename, filepath, width_px, height_px,
        dpi_x, dpi_y, print_inches, nest_group (optional).
        """
        entries = RIPLogParser.filter_recent(entries)

        file_list = []
        # Track which RIP entries have been consumed by nests
        consumed_rip = set()
        # nest "Start Printing" index -> its assigned RIP member indices (in order)
        nest_members = {}

        # First pass: assign each nest its OWN RIP members (nearest unconsumed,
        # scanning back). Marking them consumed stops the next nest from reusing
        # them. The second pass emits exactly this assignment — previously it
        # re-searched by proximity, so back-to-back nests double-counted the
        # earlier nest's members and silently dropped the later nest's oldest
        # files from the list entirely.
        for i, entry in enumerate(entries):
            if entry.get('block_type') != 'print' or not entry.get('is_nest'):
                continue
            nest_count = entry['nest_count']
            # Look backwards for unconsumed RIP entries
            members = []
            for j in range(i - 1, -1, -1):
                if j in consumed_rip:
                    continue
                if entries[j].get('block_type') != 'rip':
                    continue
                if entries[j].get('is_nest'):
                    continue
                members.append(j)
                if len(members) == nest_count:
                    break
            for idx in members:
                consumed_rip.add(idx)
            nest_members[i] = list(reversed(members))  # chronological order

        # Second pass: build file list from "Start Printing" entries only
        for i, entry in enumerate(entries):
            if entry.get('block_type') != 'print':
                continue

            if entry.get('is_nest'):
                # Nest: emit exactly the members assigned in pass 1.
                ts = entry.get('output_start', entry.get('output_end', str(i)))
                nest_group = f"riplog_nest_{ts}"
                nest_group = re.sub(r'[^a-zA-Z0-9_]', '_', nest_group)

                for idx in nest_members.get(i, []):
                    info = RIPLogParser._job_to_file_info(entries[idx])
                    if info:
                        info['nest_group'] = nest_group
                        file_list.append(info)
            else:
                # Individual file — find matching RIP entry for full path
                rip_entry = None
                for j in range(i - 1, -1, -1):
                    if entries[j].get('block_type') != 'rip':
                        continue
                    if entries[j].get('is_nest'):
                        continue
                    if j in consumed_rip:
                        continue
                    # Match by filename
                    rip_file = os.path.basename(entries[j].get('file', ''))
                    if rip_file == entry.get('file', ''):
                        rip_entry = entries[j]
                        consumed_rip.add(j)
                        break
                # Use RIP entry (has full path) if found, otherwise Printing entry
                source = rip_entry if rip_entry else entry
                info = RIPLogParser._job_to_file_info(source)
                if info:
                    file_list.append(info)

        return file_list

    @staticmethod
    def _job_to_file_info(job):
        """Convert a single parsed job to the agent file info format."""
        filepath = job.get('file', '')
        if not filepath:
            return None

        filename = os.path.basename(filepath)
        width_in = job.get('width_in', 0)
        height_in = job.get('height_in', 0)
        res_x = job.get('res_x', 300)
        res_y = job.get('res_y', 300)

        width_px = int(width_in * res_x) if width_in and res_x else 0
        height_px = int(height_in * res_y) if height_in and res_y else 0

        return {
            'filename': filename,
            'filepath': filepath,
            'width_px': width_px,
            'height_px': height_px,
            'dpi_x': res_x,
            'dpi_y': res_y,
            'print_inches': height_in,
            'copies': parse_copies(filename),
            'nest_group': None,
            'source': 'riplog',
        }


class RIPLogWatcher:
    """Watches RIPLOG.HTML for changes by polling file modification time."""

    def __init__(self, riplog_path, callback):
        self.riplog_path = riplog_path
        self.callback = callback
        self._last_mtime = 0
        self._last_size = 0
        self._last_job_count = 0
        self.running = True

        # Initial parse
        self._check_for_changes()

        # Start polling thread
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        while self.running:
            time.sleep(3)  # Check every 3 seconds
            self._check_for_changes()

    def _check_for_changes(self):
        try:
            stat = os.stat(self.riplog_path)
            mtime = stat.st_mtime
            size = stat.st_size

            if mtime != self._last_mtime or size != self._last_size:
                self._last_mtime = mtime
                self._last_size = size

                # Parse and check if job count changed
                jobs = RIPLogParser.parse_file(self.riplog_path)
                if len(jobs) != self._last_job_count:
                    self._last_job_count = len(jobs)
                    self.callback(jobs)
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"RIPLog watcher error: {e}")

    def stop(self):
        self.running = False


# ── Configuration ──


def parse_copies(filename):
    """Extract copies count from filename pattern like '(2 x)'. Returns 1 if not found."""
    m = re.search(r'\((\d+)\s*x\)', filename)
    return int(m.group(1)) if m else 1

