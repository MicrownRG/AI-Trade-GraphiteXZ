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
        Feed 'time' format: "March 15, 2026 12:30pm" (example)
        """
        if not events:
            return False

        now = datetime.now(timezone.utc)
        for e in events:
            event_time_str = f"{e.get('date')} {e.get('time')}"
            try:
                # Common format: "March 15, 2026 12:30pm"
                # Some feeds might differ, adjust parsing if needed
                event_dt = datetime.strptime(event_time_str, "%B %d, %Y %I:%M%p")
                
                diff = abs((event_dt - now).total_seconds()) / 60
                if diff <= window_minutes:
                    return True
            except:
                continue
        return False
