"""Bounded proposal selection. Scores prioritize work; only native gates accept.

UUID normalization applies exclusively to newly created routing objects. Every
original identity, rule, permission and non-routing field remains in the key.
"""
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

from .jobs import Halt, write_json
from .validation import canonical, field, name, parse_sexpr, read_board
from .workspace import source_files


def _context(board, include_board=True):
    return sorted((str(Path(p).relative_to(board.parent)) if Path(p).is_relative_to(board.parent) else p, h)
                  for p,h in source_files(board).items() if include_board or Path(p) != board)


def fingerprint(board, candidate, task):
    original = read_board(board)
    def exact(node):
        # Unlike validation.canonical, retain cached fill in this conservative
        # cache key too. Two distinct inputs must not share a failed result.
        return tuple(exact(x) for x in node) if isinstance(node, list) else canonical(node)
    root = parse_sexpr(candidate.read_text())[0]
    static, routing = [], []
    for node in root[1:]:
        if name(node) not in ('segment', 'via', 'arc'):
            static.append(exact(node)); continue
        uid = field(node, 'uuid') or field(node, 'tstamp')
        if uid and str(uid[1]) not in original['stored_uuids']:
            node = [x for x in node if name(x) not in ('uuid', 'tstamp')]
            if name(node) == 'segment':
                a, b = field(node, 'start'), field(node, 'end')
                if a and b and exact(a[1:]) > exact(b[1:]):
                    node = [['start', *b[1:]] if x is a else ['end', *a[1:]] if x is b else x for x in node]
        routing.append(exact(node))
    value = [_context(board), task, static, sorted(routing, key=repr), _context(candidate, False)]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def score(board, candidate):
    """Prefer fewer original edits, then fewer vias and shorter added copper."""
    old, new = read_board(board)['routing'], read_board(candidate)['routing']
    changed = sum(uid not in new or canonical(node) != canonical(new[uid]) for uid, node in old.items())
    added = [node for uid, node in new.items() if uid not in old]
    length = 0.0
    for node in added:
        if name(node) == 'segment':
            a, b = field(node, 'start'), field(node, 'end')
            length += math.hypot(float(a[1])-float(b[1]), float(a[2])-float(b[2]))
    return [changed, sum(name(n) == 'via' for n in added), round(length, 9), len(added)]


class ValidationJob:
    def __init__(self, parent, index):
        self.parent = parent
        self.label = 'candidate-%d' % index
        self.folder = parent.folder/'candidate-validations'/str(index)
        self.folder.mkdir(parents=True, exist_ok=False)
        for filename in ('task.json', 'task-manifest.json'):
            shutil.copy2(parent.folder/filename, self.folder/filename)
    def check(self): self.parent.check()
    def run(self, label, argv, **kwargs):
        return self.parent.run(self.label+'-'+label, argv, **kwargs)


def validate_candidates(job, config, board, candidate, task, report, budget=120, validator=None):
    from .candidate_pipeline import validate, outcome
    validator = validator or validate
    pool = report.get('candidate_pool')
    if not pool:
        code, result = validator(job, config, board, candidate, task, budget=budget)
        return candidate, code, result
    started = time.monotonic()
    pool.update(status='validating', verification_budget_seconds=budget, verification_seconds=0.0)
    baseline = source_files(board)
    def save():
        pool['verification_seconds'] = time.monotonic()-started
        write_json(job.folder/'candidate-pool.json', pool)
    try:
        last = None
        for index, row in enumerate(pool['candidates']):
            job.check()
            if row['fingerprint'] in report.get('candidate_exclusions', []):
                row.update(status='excluded_branch', reason='This exact proposal was already explored in the selected group')
                continue
            left = budget-(time.monotonic()-started)
            if left <= 0: raise Halt('timed_out', 'Candidate pool shared verification budget exhausted')
            path = Path(row['board'])
            if (source_files(board) != baseline or source_files(path) != row['hashes']
                    or fingerprint(board, path, task) != row['fingerprint']):
                raise Halt('protection_violation', 'Candidate pool input, permissions or proposal changed')
            sub = job if index == 0 else ValidationJob(job, index)
            row['verification_directory'] = str(sub.folder/'verification')
            # Record the actual candidate even when the verifier must stop.
            report['candidate_construction'] = dict(status='ready', pns_attempt=row['attempt'],
                candidate_board=str(path), requires_native_verification=True)
            report['candidate'] = str(path)
            report['verification_directory'] = row['verification_directory']
            report.pop('verification', None)
            row['status'] = 'validating'
            code, result = validator(sub, config, board, path, task, budget=left)
            status, reason = outcome(code, result)
            row.update(status=status, reason=reason, failed_checks=[k for k,v in result.get('checks',{}).items() if not v])
            report['verification'] = result
            save(); job.check()
            if source_files(board) != baseline or source_files(path) != row['hashes']:
                raise Halt('protection_violation', 'Candidate or baseline changed during pool validation')
            if time.monotonic()-started > budget:
                raise Halt('timed_out', 'Candidate pool cumulative verification budget exceeded')
            if status in ('protection_violation', 'verification_incomplete'):
                raise Halt(status, reason)
            last = path, code, result
            if status == 'accepted':
                pool.update(status='accepted', selected_attempt=row['attempt'], selected_fingerprint=row['fingerprint'])
                return last
        if last is None: raise Halt('no_progress', 'No eligible candidate in pool')
        pool['status'] = outcome(last[1], last[2])[0]
        return last
    except Halt as exc:
        pool.update(status=exc.status, reason=str(exc))
        raise
    finally:
        save()
