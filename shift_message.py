from collections import defaultdict
from datetime import datetime, timedelta, timezone

EDT = timezone(timedelta(hours=-4))


def percentage_bar(percent, width=20):
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return f"{'█' * filled}{'░' * (width - filled)} {percent:.0f}%"


def send_shift_report(client, channel_id, report):
    # Average available five-minute polls into ten-minute windows.
    buckets = defaultdict(list)

    for poll in report["polls"]:
        timestamp = datetime.fromisoformat(poll["polled_at"])
        window = int(timestamp.timestamp()) // 600 * 600
        buckets[window].append(poll["user_count"])

    points = [
        {
            "label": datetime.fromtimestamp(window, EDT).strftime("%H:%M"),
            "value": round(sum(counts) / len(counts), 2),
        }
        for window, counts in sorted(buckets.items())
    ]

    host_rows = []
    for host in report["hosts"]:
        percent = host.get("attendance_percent")
        attendance = "No attendance data" if percent is None else f"`{percentage_bar(percent)}`"
        host_rows.append(f"<@{host['user_id']}>\n{attendance}\n"
                         f"*Current UPs:* {host.get('current_ups', 'Unavailable')}/300\n"
                         f"*Leaderboard position:* "
                         f"{('#' + str(host['leaderboard_position'])) if host.get('leaderboard_position') else 'Not ranked'}")
    host_lines = "\n\n".join(host_rows) or "No scheduled hosts"

    title = "Shift Summary - " + (", ".join(
        f"<@{host['user_id']}>" for host in report["hosts"]
    ) or "No scheduled host")
    summary = (
        f"*{title}*\n"
        f"*Huddle observed:* {'Yes' if report['huddle_observed'] else 'No'}\n"
        f"*Host observed:* {'Yes' if report['host_present'] else 'No'}\n"
        f"*Unique participants:* {report['unique_participant_count']}\n"
        f"*Recorded polls:* {len(report['polls'])}\n\n"
        f"*Hosts*\n{host_lines}"
    )

    blocks = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": summary},
    }]

    # Up to two charts, each with 20 points, in a Slack message.
    # Weekend shifts last up to five hours (30 ten-minute windows).
    if len(points) > 40:
        raise ValueError("Report exceeds two charts; split into multiple messages")
    for offset in range(0, len(points), 20):
        chart_points = points[offset:offset + 20]
        blocks.append({
            "type": "data_visualization",
            "title": "Average participants per 10-minute window",
            "chart": {
                "type": "area",
                "series": [{
                    "name": "Participants",
                    "data": chart_points,
                }],
                "axis_config": {
                    "categories": [p["label"] for p in chart_points],
                    "x_label": "Window start (EDT)",
                    "y_label": "Average participants",
                },
            },
        })

    blocks.append({
        "type": "context",
        "elements": [{
            "type": "mrkdwn",
            "text": (
                "Attendance reflects saved polls only. "
                "Missing windows are omitted, not counted as zero."
            ),
        }],
    })

    return client.chat_postMessage(
        channel=channel_id,
        text=(
            f"{title}: "
            f"{report['unique_participant_count']} unique participants"
        ),
        blocks=blocks,
        unfurl_links=False,
        unfurl_media=False,
    )
