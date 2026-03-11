#!/usr/bin/env python3
"""
Static analysis tool to validate thread-safety patterns in server.py

Checks for:
1. All self.clients accesses are protected
2. All DSP operations use locks
3. All file handle operations use locks
4. No lock acquisitions in wrong order (deadlock prevention)
"""

import re
from pathlib import Path

def check_file(filepath):
    """Perform static analysis on server.py for thread-safety patterns."""
    
    content = Path(filepath).read_text()
    lines = content.split('\n')
    
    issues = []
    warnings = []
    
    print(f"Analyzing {filepath} for thread-safety patterns...")
    print("=" * 70)
    
    # Check 1: Unprotected self.clients access
    print("\n[1] Checking for unprotected self.clients access...")
    clients_pattern = re.compile(r'\bself\.clients(?!\s*=\s*\{)')
    in_lock_block = False
    lock_depth = 0
    
    for i, line in enumerate(lines, 1):
        # Track lock context
        if 'with self._clients_lock:' in line:
            in_lock_block = True
            lock_depth = line.index('with')
        elif in_lock_block and line.strip() and not line.strip().startswith('#'):
            current_depth = len(line) - len(line.lstrip())
            if current_depth <= lock_depth:
                in_lock_block = False
        
        # Check for clients access
        if clients_pattern.search(line):
            # Exceptions: initialization, comments
            if 'def __init__' in lines[max(0, i-10):i] or line.strip().startswith('#'):
                continue
            if not in_lock_block:
                issues.append(f"Line {i}: Unprotected self.clients access\n  {line.strip()}")
    
    if not issues:
        print("  ✅ All self.clients accesses appear protected")
    else:
        print(f"  ⚠️  Found {len(issues)} potential issues:")
        for issue in issues[:5]:  # Show first 5
            print(f"    {issue}")
    
    # Check 2: DSP operations without locks
    print("\n[2] Checking DSP operations for lock protection...")
    dsp_ops = ['compute_fft', 'demodulate', 'set_mode', 'set_squelch']
    unprotected_dsp = []
    
    for i, line in enumerate(lines, 1):
        for op in dsp_ops:
            if f'self.dsp.{op}' in line or f'dsp.{op}' in line:
                # Check if within dsp_lock context
                context_start = max(0, i - 5)
                context = '\n'.join(lines[context_start:i])
                
                if 'with' in context and 'dsp_lock' in context:
                    continue
                if 'def _process_chunk' in context and op in ['compute_fft', 'demodulate']:
                    continue  # Should be protected inside _process_chunk
                    
                unprotected_dsp.append(f"Line {i}: {op} without dsp_lock?\n  {line.strip()}")
    
    if not unprotected_dsp:
        print("  ✅ All DSP operations appear protected")
    else:
        print(f"  ℹ️  Found {len(unprotected_dsp)} potential unlocked DSP operations")
        print("     (Some may be false positives - manual review recommended)")
    
    # Check 3: File handle operations
    print("\n[3] Checking file handle operations...")
    file_ops = ['iq_capture_file.write', 'audio_wav_file.write', 'iq_file.write', 'audio_wav.write']
    unprotected_file = []
    
    for i, line in enumerate(lines, 1):
        for op in file_ops:
            if op in line:
                context_start = max(0, i - 5)
                context = '\n'.join(lines[context_start:i])
                
                if 'with' in context and 'recording_lock' in context:
                    continue
                    
                unprotected_file.append(f"Line {i}: File operation without recording_lock\n  {line.strip()}")
    
    if not unprotected_file:
        print("  ✅ All file operations appear protected")
    else:
        print(f"  ⚠️  Found {len(unprotected_file)} potential unprotected file operations")
    
    # Check 4: Lock acquisition patterns (deadlock prevention)
    print("\n[4] Checking for potential deadlock patterns...")
    nested_locks = []
    
    for i, line in enumerate(lines, 1):
        if 'with self._' in line and 'lock' in line:
            # Look for nested lock within next 10 lines
            context = '\n'.join(lines[i:i+10])
            if context.count('with self._') > 1:
                nested_locks.append(f"Line {i}: Potential nested lock acquisition")
    
    if not nested_locks:
        print("  ✅ No nested lock patterns detected")
    else:
        print(f"  ℹ️  Found {len(nested_locks)} nested lock patterns (review for deadlock risk)")
    
    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    total_issues = len(issues) + len(unprotected_file)
    total_warnings = len(unprotected_dsp) + len(nested_locks)
    
    if total_issues == 0 and total_warnings == 0:
        print("✅ All checks passed! No thread-safety issues detected.")
    elif total_issues == 0:
        print(f"⚠️  {total_warnings} warnings (manual review recommended)")
    else:
        print(f"❌ {total_issues} issues found, {total_warnings} warnings")
    
    print()
    return total_issues == 0

if __name__ == "__main__":
    success = check_file("server.py")
    exit(0 if success else 1)
