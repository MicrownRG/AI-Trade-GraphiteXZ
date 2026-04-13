"""
Report Formatter — Converts AI Learning JSON into user-friendly Telegram Markdown.
"""
from datetime import datetime
from telegram.formatter import _escape

def format_eod_report(data: dict) -> str:
    """Formats the AI Retrospective into a MarkdownV2 message."""
    summary = data.get("summary", "No summary provided.")
    grade = data.get("overall_grade", "N/A")
    patterns = data.get("patterns", [])
    
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = f"🎓 *AI PERFORMANCE RETROSPECTIVE*\n_{_escape(now_str)}_\n\n"
    
    report += f"📊 *Grade: {_escape(grade)}*\n"
    report += f"📝 *Executive Summary:*\n_{_escape(summary)}_\n\n"
    
    if patterns:
        report += "🔍 *Detected Patterns:*\n"
        for p in patterns:
            p_type = p.get("type", "PATTERN")
            emoji = "✅" if p_type == "WINNING" else "❌" if p_type == "LOSING" else "👀"
            
            insight = p.get("insight", "")
            rec = p.get("recommendation", "Continue current logic.")
            impact = p.get("score_impact", 0)
            impact_str = f"[{impact:+}]" if impact != 0 else ""
            
            report += f"{emoji} *{_escape(p_type)} {_escape(impact_str)}*\n"
            report += f"└ {_escape(insight)}\n"
            report += f"└ _Suggestion: {_escape(rec)}_\n\n"
    else:
        report += "⚠️ _No clear patterns detected in this sample\\._\n"
        
    report += "\n\\#XAUUSD \\#AI\\_Retrospective"
    
    return report
