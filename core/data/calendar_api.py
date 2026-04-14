import requests
from typing import List, Dict
from datetime import datetime, timezone

class CalendarAPI:
    def __init__(self):
        # Using a common public JSON feed for economic calendar
        self.url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def get_high_impact_events(self) -> List[Dict]:
        """
        Fetches high-impact events for the current day.
        """
        try:
            r = requests.get(self.url, timeout=10)
            if r.status_code == 200:
                events = r.json()
                today = datetime.now().strftime("%Y-%m-%d")
                
                high_impact = []
                for e in events:
                    # Filter high impact and relevant currency
                    if e.get("impact") == "High" and e.get("country") in ["USD", "ALL"]:
                        # Convert date if necessary to check if it's today
                        # The feed format is usually M-D-Y or similar
                        high_impact.append({
                            "title": e.get("title"),
                            "time": e.get("date"),
                            "impact": e.get("impact")
                        })
                return high_impact
        except Exception as e:
            # Fallback or log error
            return []

    def is_news_active(self, events: List[Dict], window_minutes: int = 30) -> bool:
        """
        Checks if any high-impact event is within the window from now.
        Feed 'time' format: ISO8601 (e.g. '2026-04-13T08:30:00-04:00')
        """
        if not events:
            return False

        now = datetime.now(timezone.utc)
        for e in events:
            try:
                # Some feeds provide standard ISO datetime with timezone
                date_str = str(e.get('time', ''))
                if not date_str:
                    continue
                    
                # Support standard ISO format decoding
                event_dt = datetime.fromisoformat(date_str)
                # Convert to UTC if needed
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
                else:
                    event_dt = event_dt.astimezone(timezone.utc)
                    
                diff = abs((event_dt - now).total_seconds()) / 60
                if diff <= window_minutes:
                    return True
            except Exception:
                continue
        return False
