"""Opt-in five-listing Maps smoke; never overrides the Google-source gate."""
import argparse
import importlib.util
import json
from pathlib import Path
from app.maps_browser import MapsBrowserError, MapsBrowserProvider, source_preflight
from app.providers import DiscoveryRequest
from app.schemas import CampaignInput


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly request the live smoke')
    parser.add_argument('--config', type=Path, required=True, help='Campaign JSON with declared source handling')
    args = parser.parse_args()
    blockers = []
    if not args.live:
        blockers.append('Live smoke requires explicit --live opt-in.')
    if importlib.util.find_spec('playwright') is None:
        blockers.append('Playwright is not installed; install .[maps] and Chromium.')
    try:
        data = CampaignInput.model_validate_json(args.config.read_text(encoding='utf-8'))
        if data.provider_config.discovery != 'maps_browser':
            blockers.append('Configuration must select maps_browser.')
        source_preflight()
    except MapsBrowserError as exc:
        blockers.append(str(exc))
    except Exception:
        blockers.append('Invalid/unreadable campaign configuration; no raw configuration logged.')
    if blockers:
        print(json.dumps({'status': 'NOT RUN', 'query': 'nail salon Yerevan', 'maximum_listings': 5,
            'blockers': blockers}))
        return 2
    # This branch is unreachable under the current policy. It is ready for an
    # explicitly reviewed future source-policy change; it never grants rights.
    config = data.provider_config.model_copy(update={'maps_listing_limit': 5, 'maps_search_budget': 1, 'maps_visit_budget': 5})
    provider = MapsBrowserProvider(config)
    request = DiscoveryRequest(0, 'nail salon Yerevan', 'en', 'nail salon', 'Yerevan', None)
    cursor, count, steps = None, 0, 0
    try:
        while steps < config.maps_scroll_round_limit + 6:
            page = provider.discover_page(request, cursor, 1)
            count += len(page.results)
            steps += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        if cursor is not None:
            raise MapsBrowserError('timeout')
        print(json.dumps({'status': 'PASS', 'maximum_listings': 5, 'parsed_count': count,
            'searches': provider.searches, 'listing_visits': provider.visits, 'persistence': 'none'}))
        return 0
    except MapsBrowserError as exc:
        print(json.dumps({'status': 'FAIL', 'error_code': exc.code, 'reason': str(exc), 'parsed_count': count}))
        return 1
    finally:
        provider.close()


if __name__ == '__main__':
    raise SystemExit(main())
