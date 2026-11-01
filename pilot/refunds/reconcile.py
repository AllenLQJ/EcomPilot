"""Recover callbacks whose delivery outcome was uncertain."""
import httpx


def reconcile_items(items, base_url, internal_token, client=None):
    owned = client is None
    client = client if client is not None else httpx.Client(timeout=10)
    results = []
    headers = {'X-Internal-Token': internal_token}
    base_url = base_url.rstrip('/')
    try:
        for item in items:
            key = item['refund_idempotency_key']
            outcome, status = 'RETRY_LATER', None
            try:
                response = client.get(base_url + '/internal/v1/refunds/status',
                                      params={'refund_idempotency_key': key}, headers=headers)
                response.raise_for_status()
                status = response.json()['execution_status']
                if status == 'SUCCESS':
                    outcome = 'RECONCILED'
                elif status == 'NOT_FOUND':
                    response = client.post(base_url + '/internal/v1/refunds/execute',
                                           json=item['execute_payload'], headers=headers)
                    response.raise_for_status()
                    status = response.json()['execution_status']
                    if status == 'SUCCESS':
                        outcome = 'REPLAYED'
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
            results.append(dict(refund_idempotency_key=key, outcome=outcome, execution_status=status))
    finally:
        if owned:
            client.close()
    return results
