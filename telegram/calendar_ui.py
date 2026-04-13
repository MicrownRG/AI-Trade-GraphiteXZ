"""
Simplified Telegram Inline Calendar.
Generates an inline keyboard for date selection.
"""
from datetime import datetime
import calendar

def create_calendar(year: int = None, month: int = None):
    now = datetime.now()
    if year is None: year = now.year
    if month is None: month = now.month

    keyboard = []
    
    # Header: Month and Year
    month_name = calendar.month_name[month]
    keyboard.append([
        {"text": f"{month_name} {year}", "callback_data": "ignore"}
    ])
    
    # Weekday headers
    weekdays = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    keyboard.append([{"text": day, "callback_data": "ignore"} for day in weekdays])
    
    # Days
    cal = calendar.monthcalendar(year, month)
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append({"text": " ", "callback_data": "ignore"})
            else:
                # Format: cal:YYYY:MM:DD
                row.append({
                    "text": str(day), 
                    "callback_data": f"cal:set:{year}:{month}:{day}"
                })
        keyboard.append(row)
    
    # Navigation
    prev_m = month - 1 if month > 1 else 12
    prev_y = year if month > 1 else year - 1
    next_m = month + 1 if month < 12 else 1
    next_y = year if month < 12 else year + 1
    
    keyboard.append([
        {"text": "⬅️", "callback_data": f"cal:nav:{prev_y}:{prev_m}"},
        {"text": "Today", "callback_data": f"cal:set:{now.year}:{now.month}:{now.day}"},
        {"text": "➡️", "callback_data": f"cal:nav:{next_y}:{next_m}"}
    ])
    
    return {"inline_keyboard": keyboard}
