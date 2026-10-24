"""Check public UI resources and latency without creating paid inference work."""
import json
import os
import subprocess

from ops.admin_api import ROOT, read_env
from ops.checks.site_latency import measure
from image_hub.response_latency import PREFIX, summarize


def model_latency():
    # Observe already-billed short requests. Never generate monitoring traffic
    # against a paid provider, and do not confuse a gateway event with real text.
    values = read_env(ROOT/'deployment/private/config/runtime.env')
    query = """SELECT COALESCE(jsonb_agg(row_to_json(m) ORDER BY m.model),'[]') FROM (
      SELECT model,COUNT(*) AS samples,
        ROUND(percentile_cont(.5) WITHIN GROUP (ORDER BY first_token_ms)) AS first_event_p50_ms,
        ROUND(percentile_cont(.95) WITHIN GROUP (ORDER BY first_token_ms)) AS first_event_p95_ms
      FROM usage_logs WHERE created_at>NOW()-INTERVAL '15 minutes'
        AND first_token_ms IS NOT NULL AND input_tokens<=4096
        AND model NOT IN ('image_recognition','web_search','image_generation')
      GROUP BY model HAVING COUNT(*)>=3) m"""
    result = subprocess.run([str(ROOT/'runtime/bin/psql'), '-X', '-qAt', '-h', values['DATABASE_HOST'],
        '-p', values['DATABASE_PORT'], '-U', values['DATABASE_USER'], '-d', values['DATABASE_DBNAME'], '-c', query],
        env=dict(os.environ, PGPASSWORD=values['DATABASE_PASSWORD'], PGCONNECT_TIMEOUT='3',
                 PGOPTIONS='-c statement_timeout=2000'), check=True, capture_output=True, text=True, timeout=5)
    return json.loads(result.stdout)


def response_latency():
    result = subprocess.run(['journalctl', '--user', '-u', 'sub2api-agent-images.service',
        '--since', '15 minutes ago', '-n', '10000', '-o', 'cat', '--no-pager', '--grep', '^'+PREFIX],
        check=False, capture_output=True, text=True, timeout=3)
    if result.returncode == 1 and not result.stdout:
        return []  # A newly restored service has no response observations yet.
    result.check_returncode()
    records = []
    for line in result.stdout.splitlines():
        if line.startswith(PREFIX):
            try:
                records.append(json.loads(line[len(PREFIX):]))
            except ValueError:
                pass
    return summarize(records)


def main():
    config = json.loads((ROOT/'deployment/site.json').read_text())
    destination = ROOT/'var/reports/latency-current.json'
    try:
        previous = json.loads(destination.read_text())
    except (OSError, ValueError):
        previous = {}
    report = measure(config['public_url'], samples=1, head_assets=True, request_timeout=4)
    try:
        report['model_latency'] = model_latency()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report['model_latency'] = []
        report['model_observation_error'] = type(error).__name__
    report['slow_models'] = [row['model'] for row in report['model_latency'] if row['first_event_p95_ms'] > 10000]
    try:
        report['response_latency'] = response_latency()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        report['response_latency'] = []
        report['response_observation_error'] = type(error).__name__
    report['slow_models'] = sorted(set(report['slow_models']) | {
        row['model'] for row in report['response_latency'] if row['streaming'] and not row['with_tools']
        and (row['first_text_p95_ms'] > 10000 or row['semantic_gap_p95_ms'] > 10000)})
    slow = [row['origin']+row['endpoint'] for row in report['checks']
            if row['p95_ms'] > (2000 if row['origin'] == 'public' else 250)]
    report['slow_endpoints'] = slow
    bad = not report['all_calls_passed'] or bool(slow)
    report['consecutive_failures'] = previous.get('consecutive_failures', 0)+1 if bad else 0
    report['state'] = ('unhealthy' if report['consecutive_failures'] >= 3 else 'degraded') if bad else 'healthy'
    if report['state'] == 'healthy' and (report['slow_models'] or report.get('model_observation_error') or report.get('response_observation_error')):
        report['state'] = 'degraded'
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_suffix('.pending')
    pending.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    pending.chmod(0o600)
    os.replace(pending, destination)
    if report['state'] != previous.get('state') or report['state'] == 'unhealthy':
        print(json.dumps({'event': 'site_latency_state', 'state': report['state'],
                          'entry_resources_ok': all(r['ok'] for r in report['entries']),
                          'slow_endpoints': slow, 'slow_models': report['slow_models'],
                          'model_observation_error': report.get('model_observation_error'),
                          'response_observation_error': report.get('response_observation_error'),
                          'consecutive_failures': report['consecutive_failures']}), flush=True)
    if report['state'] == 'unhealthy':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
