#!/usr/bin/env python3
"""
ADIFPUSH - Enhanced Python Version
Auto-upload WSJT-X/JTDX QSO logs to Cloudlog with duplicate detection
Replaces .NET Core 3.1 version with zero compilation hassles
"""

import sys
import os
import json
import socket
import struct
import time
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from datetime import datetime
from urllib.parse import urljoin
import requests
from requests.exceptions import RequestException


class Config:
    """Handles configuration file management"""
    
    CONFIG_DIR = Path.home() / ".adifpush"
    CONFIG_FILE = CONFIG_DIR / "cloudlog"
    CACHE_FILE = CONFIG_DIR / "uploaded_qsos"  # Track uploaded QSOs
    
    @staticmethod
    def ensure_dir():
        Config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    @staticmethod
    def save(url: str, api_key: str, station_id: str):
        """Save configuration"""
        Config.ensure_dir()
        content = f"url={url}\napikey={api_key}\nstationid={station_id}\n"
        Config.CONFIG_FILE.write_text(content)
        print(f"✓ Configuration saved to {Config.CONFIG_FILE}")
    
    @staticmethod
    def load() -> Optional[Dict[str, str]]:
        """Load configuration"""
        if not Config.CONFIG_FILE.exists():
            return None
        
        config = {}
        for line in Config.CONFIG_FILE.read_text().strip().split('\n'):
            if '=' in line:
                key, value = line.split('=', 1)
                config[key.strip()] = value.strip()
        
        if all(k in config for k in ['url', 'apikey', 'stationid']):
            return config
        return None
    
    @staticmethod
    def get_wsjtx_log_path() -> Path:
        """Get WSJT-X log file path (Windows AppData)"""
        if sys.platform == 'win32':
            app_data = Path.home() / "AppData" / "Local" / "WSJT-X"
        elif sys.platform == 'darwin':
            # macOS
            app_data = Path.home() / "Library" / "Application Support" / "WSJT-X"
        else:
            # Linux
            app_data = Path.home() / ".local" / "share" / "WSJT-X"
        
        log_file = app_data / "wsjtx_log.adi"
        return log_file
    
    @staticmethod
    def load_uploaded_qsos() -> Set[str]:
        """Load set of already uploaded QSO hashes"""
        Config.ensure_dir()
        if not Config.CACHE_FILE.exists():
            return set()
        
        try:
            return set(Config.CACHE_FILE.read_text().strip().split('\n'))
        except:
            return set()
    
    @staticmethod
    def save_uploaded_qso(qso_hash: str):
        """Add QSO hash to uploaded list"""
        Config.ensure_dir()
        uploaded = Config.load_uploaded_qsos()
        uploaded.add(qso_hash)
        Config.CACHE_FILE.write_text('\n'.join(sorted(uploaded)))


class AdifParser:
    """Parse ADIF format QSO records"""
    
    @staticmethod
    def parse_line(line: str) -> Optional[Dict]:
        """Parse single ADIF line into dictionary"""
        record = {}
        
        # Match ADIF tags: <KEY:LENGTH>VALUE
        pattern = r'<(\w+):(\d+)>([^<]*)'
        matches = re.findall(pattern, line, re.IGNORECASE)
        
        if not matches:
            return None
        
        for key, length, value in matches:
            record[key.lower()] = value
        
        # Validate essential fields
        if 'call' not in record or 'qso_date' not in record or 'time_on' not in record:
            return None
        
        return record
    
    @staticmethod
    def calculate_hash(adif_line: str) -> str:
        """Calculate SHA256 hash of ADIF record for duplicate detection"""
        # Parse to normalize the record (remove whitespace differences)
        record = AdifParser.parse_line(adif_line)
        if not record:
            return ""
        
        # Create normalized string for hashing (date + time + call + freq + mode)
        normalized = f"{record.get('qso_date', '')}_{record.get('time_on', '')}_" \
                    f"{record.get('call', '')}_{record.get('freq', '')}_{record.get('mode', '')}"
        return hashlib.sha256(normalized.encode()).hexdigest()
    
    @staticmethod
    def read_file(filepath: str) -> List[str]:
        """Read ADIF file, handling file locks with retries"""
        max_retries = 5
        for attempt in range(max_retries):
            try:
                with open(filepath, 'r') as f:
                    return f.readlines()
            except IOError as e:
                if attempt < max_retries - 1:
                    time.sleep(0.5)  # File locked, retry
                else:
                    raise
    
    @staticmethod
    def read_new_records(filepath: str, last_upload_time: Optional[datetime] = None) -> List[str]:
        """Read only new ADIF records from file (those after last upload)"""
        try:
            lines = AdifParser.read_file(filepath)
        except IOError as e:
            return []
        
        new_records = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            record = AdifParser.parse_line(line)
            if not record:
                continue
            
            # If we're tracking by time, only include records after last upload
            if last_upload_time:
                try:
                    qso_date = record.get('qso_date', '')
                    time_on = record.get('time_on', '')
                    if qso_date and time_on:
                        record_dt = datetime.strptime(f"{qso_date}{time_on}", "%Y%m%d%H%M%S")
                        if record_dt < last_upload_time:
                            continue
                except:
                    pass
            
            new_records.append(line)
        
        return new_records


class CloudlogPusher:
    """Push ADIF records to Cloudlog via HTTP API"""
    
    def __init__(self, config: Dict[str, str]):
        self.url = config['url']
        self.api_key = config['apikey']
        self.station_id = config['stationid']
        self.session = requests.Session()
        self.endpoint = urljoin(self.url, '/index.php/api/qso')
    
    def push_record(self, adif_line: str, show_progress: bool = False) -> Tuple[bool, Optional[str]]:
        """Push single ADIF record to Cloudlog"""
        
        # Parse ADIF line
        record = AdifParser.parse_line(adif_line)
        if not record:
            return False, "Invalid ADIF format"
        
        call = record.get('call', 'UNKNOWN')
        if show_progress:
            print(f"  {call}... ", end='', flush=True)
        
        # Clean up TX power field (remove 'W' suffix)
        if 'tx_pwr' in record:
            record['tx_pwr'] = record['tx_pwr'].replace('W', '').strip()
        
        # Prepare JSON payload
        payload = {
            "key": self.api_key,
            "station_profile_id": self.station_id,
            "type": "adif",
            "string": adif_line
        }
        
        try:
            response = self.session.post(
                self.endpoint,
                json=payload,
                timeout=10
            )
            
            if show_progress:
                print(response.status_code)
            
            if response.status_code in (200, 201):
                return True, None
            else:
                return False, f"{response.status_code}: {response.text[:100]}"
        
        except RequestException as e:
            if show_progress:
                print(f"ERROR: {e}")
            return False, str(e)
    
    def push_file(self, filepath: str, show_progress: bool = True, skip_duplicates: bool = True) -> Dict:
        """Push ADIF file to Cloudlog with duplicate detection"""
        
        if show_progress:
            print(f"\nReading {filepath}...")
        
        try:
            lines = AdifParser.read_file(filepath)
        except IOError as e:
            print(f"✗ Cannot read file: {e}")
            return {"success": 0, "failed": 0, "skipped": 0}
        
        if show_progress:
            print(f"POSTing to {self.endpoint}")
        
        # Load previously uploaded QSOs
        uploaded_hashes = Config.load_uploaded_qsos() if skip_duplicates else set()
        
        success = 0
        failed = 0
        skipped = 0
        errors = []
        
        for idx, line in enumerate(lines, 1):
            trimmed = line.strip()
            if not trimmed or trimmed.startswith("#"):
                continue
            
            # Check for duplicate
            qso_hash = AdifParser.calculate_hash(trimmed)
            if qso_hash and qso_hash in uploaded_hashes:
                skipped += 1
                continue
            
            result = self.push_record(trimmed, show_progress)
            
            if result[0]:
                success += 1
                if qso_hash:
                    Config.save_uploaded_qso(qso_hash)
            else:
                failed += 1
                errors.append(f"  Line {idx}: {result[1]}")
        
        if show_progress:
            print(f"\n✓ {success} successful, ✗ {failed} failed, ⊘ {skipped} skipped (duplicates)")
            for error in errors[:5]:  # Show first 5 errors
                print(error)
            if len(errors) > 5:
                print(f"  ... and {len(errors) - 5} more errors")
        
        return {"success": success, "failed": failed, "skipped": skipped}


class WsjtxListener:
    """Listen to WSJT-X UDP multicast messages"""
    
    MULTICAST_GROUP = '239.255.0.1'
    MULTICAST_PORT = 2237
    
    def __init__(self, pusher: CloudlogPusher):
        self.pusher = pusher
        self.socket = None
        self.last_uploaded_qsos: Set[str] = Config.load_uploaded_qsos()
    
    def start(self):
        """Start listening for WSJT-X messages"""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Bind to multicast port
        self.socket.bind(('', self.MULTICAST_PORT))
        
        # Join multicast group
        mreq = struct.pack('4sL', socket.inet_aton(self.MULTICAST_GROUP), socket.INADDR_ANY)
        self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        print(f"✓ Listening on {self.MULTICAST_GROUP}:{self.MULTICAST_PORT}")
        print("  Waiting for WSJT-X QSOs... (Ctrl+C to exit)\n")
        
        try:
            while True:
                data, addr = self.socket.recvfrom(65535)
                self._parse_message(data)
        except KeyboardInterrupt:
            print("\n✓ Shutting down...")
        finally:
            self.socket.close()
    
    def _parse_message(self, data: bytes):
        """Parse WSJT-X UDP message and extract ADIF if present"""
        try:
            # Look for ADIF patterns
            text_data = data.decode('utf-8', errors='ignore')
            if '<QSO_DATE:' not in text_data or '<CALL:' not in text_data:
                return
            
            # Extract ADIF record
            adif_match = re.search(r'<QSO_DATE:\d+>\d+.*?<EOR>', text_data)
            if not adif_match:
                return
            
            adif_line = adif_match.group(0)
            
            # Check for duplicate
            qso_hash = AdifParser.calculate_hash(adif_line)
            if qso_hash in self.last_uploaded_qsos:
                return  # Skip duplicate
            
            # Parse and push
            record = AdifParser.parse_line(adif_line)
            if not record:
                return
            
            result = self.pusher.push_record(adif_line, show_progress=False)
            
            if result[0]:
                call = record.get('call', 'UNKNOWN')
                print(f"✓ Uploaded QSO with {call}")
                if qso_hash:
                    Config.save_uploaded_qso(qso_hash)
                    self.last_uploaded_qsos.add(qso_hash)
            else:
                print(f"✗ Error uploading: {result[1]}")
        
        except Exception as e:
            pass  # Silently skip malformed messages


def configure_interactive():
    """Interactive configuration setup"""
    print("\n" + "="*50)
    print("ADIFPUSH Configuration")
    print("="*50 + "\n")
    
    url = input("Cloudlog URL (e.g., https://cloudlog.example.com): ").strip()
    if not url.startswith('http'):
        url = 'https://' + url
    
    api_key = input("API Key (from Cloudlog Admin menu): ").strip()
    station_id = input("Station ID (number from station profile URL): ").strip()
    
    if not all([url, api_key, station_id]):
        print("✗ All fields required")
        return
    
    Config.save(url, api_key, station_id)
    
    print("\n" + "="*50)
    print("Setup complete!")
    print("="*50)
    print("\nNext steps:")
    print("  Run: python adifpush.py")
    print("  Choose option 1 to start listening\n")


def show_menu(wsjtx_log_path: Path):
    """Display main menu"""
    print("\n" + "="*50)
    print("ADIFPUSH - Enhanced")
    print("="*50)
    print("\nOptions:")
    print("  1. Start listening (auto-upload from WSJT-X)")
    print("  2. Configure Cloudlog")
    print("  3. Upload ADIF file")
    
    if wsjtx_log_path.exists():
        print(f"  4. Manual sync WSJT-X log ({wsjtx_log_path.name})")
    
    print("  5. Clear duplicate cache")
    print("  Q. Quit")
    print("="*50)


def main():
    """Main entry point with menu system"""
    
    # Check for command-line arguments (backward compatibility)
    if '--configure' in sys.argv or '-c' in sys.argv:
        configure_interactive()
        return
    
    # Get WSJT-X log path
    wsjtx_log_path = Config.get_wsjtx_log_path()
    
    # Load config
    config = Config.load()
    if not config:
        print("✗ Configuration not found")
        print("  Run: python adifpush.py 2 (to configure)\n")
        return
    
    pusher = CloudlogPusher(config)
    
    print(f"\n✓ Cloudlog: {config['url']}")
    print(f"✓ Station ID: {config['stationid']}")
    
    if not wsjtx_log_path.exists():
        print(f"⚠ Warning: WSJT-X log not found at {wsjtx_log_path}")
        print("  This is normal if WSJT-X hasn't been used yet\n")
    
    # Menu loop
    while True:
        show_menu(wsjtx_log_path)
        
        choice = input("\nChoice: ").strip().lower()
        
        if choice == '1':
            # Listen mode
            listener = WsjtxListener(pusher)
            listener.start()
        
        elif choice == '2':
            # Configure
            configure_interactive()
        
        elif choice == '3':
            # Upload file
            print("\nUpload ADIF file")
            filepath = input("Enter file path: ").strip()
            
            if not filepath:
                print("✗ No file specified")
                continue
            
            if not os.path.isfile(filepath):
                print(f"✗ File not found: {filepath}")
                continue
            
            result = pusher.push_file(filepath, show_progress=True, skip_duplicates=True)
            input("\nPress Enter to continue...")
        
        elif choice == '4' and wsjtx_log_path.exists():
            # Manual sync WSJT-X log
            print(f"\nSyncing {wsjtx_log_path}...")
            result = pusher.push_file(str(wsjtx_log_path), show_progress=True, skip_duplicates=True)
            input("\nPress Enter to continue...")
        
        elif choice == '5':
            # Clear cache
            Config.ensure_dir()
            Config.CACHE_FILE.unlink(missing_ok=True)
            print("✓ Duplicate cache cleared")
            time.sleep(1)
        
        elif choice == 'q':
            print("✓ Goodbye!")
            break
        
        else:
            print("✗ Invalid choice")


if __name__ == '__main__':
    main()
