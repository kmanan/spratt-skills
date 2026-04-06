#!/bin/bash
# Outlook Calendar Operations
# Usage: outlook-calendar.sh [--account NAME] <command> [args]

BASE_DIR="$HOME/.outlook-mcp"

# Parse --account and --calendar flags
ACCOUNT="${OUTLOOK_ACCOUNT:-default}"
CAL_ID=""
while true; do
    case "$1" in
        --account|-a) ACCOUNT="$2"; shift 2;;
        --calendar|-c) CAL_ID="$2"; shift 2;;
        *) break;;
    esac
done

# Migrate legacy config to "default" subdirectory
if [ -f "$BASE_DIR/credentials.json" ] && [ ! -d "$BASE_DIR/default" ]; then
    mkdir -p "$BASE_DIR/default"
    mv "$BASE_DIR/config.json" "$BASE_DIR/default/" 2>/dev/null
    mv "$BASE_DIR/credentials.json" "$BASE_DIR/default/" 2>/dev/null
fi

CONFIG_DIR="$BASE_DIR/$ACCOUNT"
CREDS_FILE="$CONFIG_DIR/credentials.json"

# Load token (auto-refresh if expired)
CONFIG_FILE="$CONFIG_DIR/config.json"
ACCESS_TOKEN=$(jq -r '.access_token' "$CREDS_FILE" 2>/dev/null)

if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "null" ]; then
    echo "Error: Account '$ACCOUNT' not configured. Run: outlook-setup.sh --account $ACCOUNT"
    exit 1
fi

API="https://graph.microsoft.com/v1.0/me"

# Calendar path — resolve name to ID (case-insensitive), or use raw ID, or default
CAL_PATH="calendar"
if [ -n "$CAL_ID" ]; then
    # If it looks like a Graph API ID (contains = or is very long), use directly
    if echo "$CAL_ID" | grep -q '[=]' || [ ${#CAL_ID} -gt 30 ]; then
        CAL_PATH="calendars/$CAL_ID"
    else
        # Resolve by name (case-insensitive)
        _resolved=$(curl -s "$API/calendars?\$select=id,name" \
            -H "Authorization: Bearer $ACCESS_TOKEN" | \
            jq -r --arg name "$CAL_ID" '.value[] | select(.name | ascii_downcase == ($name | ascii_downcase)) | .id' | head -1)
        if [ -n "$_resolved" ]; then
            CAL_PATH="calendars/$_resolved"
        else
            echo "Error: Calendar '$CAL_ID' not found. Available calendars:"
            curl -s "$API/calendars?\$select=name" \
                -H "Authorization: Bearer $ACCESS_TOKEN" | jq -r '.value[].name'
            exit 1
        fi
    fi
fi

# Quick test — if token is expired, refresh it
_test=$(curl -s -o /dev/null -w "%{http_code}" "$API" -H "Authorization: Bearer $ACCESS_TOKEN" 2>/dev/null)
if [ "$_test" = "401" ]; then
    CLIENT_ID=$(jq -r '.client_id' "$CONFIG_FILE")
    CLIENT_SECRET=$(jq -r '.client_secret' "$CONFIG_FILE")
    REFRESH_TOKEN=$(jq -r '.refresh_token' "$CREDS_FILE")
    RESPONSE=$(curl -s -X POST "https://login.microsoftonline.com/consumers/oauth2/v2.0/token" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET&refresh_token=$REFRESH_TOKEN&grant_type=refresh_token&scope=https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/Mail.Send https://graph.microsoft.com/Calendars.ReadWrite offline_access")
    if echo "$RESPONSE" | jq -e '.access_token' > /dev/null 2>&1; then
        echo "$RESPONSE" > "$CREDS_FILE"
        ACCESS_TOKEN=$(jq -r '.access_token' "$CREDS_FILE")
    else
        echo "Error: Token refresh failed for '$ACCOUNT'" >&2
        exit 1
    fi
fi

# Detect timezone: use OUTLOOK_TZ env var, or system timezone, or fallback to UTC
if [ -n "$OUTLOOK_TZ" ]; then
    TIMEZONE="$OUTLOOK_TZ"
elif [ -f /etc/timezone ]; then
    TIMEZONE=$(cat /etc/timezone)
elif command -v timedatectl &> /dev/null; then
    TIMEZONE=$(timedatectl show --property=Timezone --value 2>/dev/null)
elif [ -L /etc/localtime ]; then
    TIMEZONE=$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')
else
    # macOS fallback
    TIMEZONE=$(ls -l /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')
fi
TIMEZONE="${TIMEZONE:-UTC}"

# Helper: Find full event ID by suffix (safely using --arg)
find_event_id() {
    local EVENT_ID="$1"
    curl -s "$API/$CAL_PATH/events?\$top=50&\$select=id" \
        -H "Authorization: Bearer $ACCESS_TOKEN" | jq -r --arg id "$EVENT_ID" '.value[] | select(.id | endswith($id)) | .id' | head -1
}

case "$1" in
    events)
        # List upcoming events
        COUNT=${2:-10}
        curl -s "$API/$CAL_PATH/events?\$top=$COUNT&\$orderby=start/dateTime%20desc&\$select=id,subject,start,end,location,isAllDay" \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Prefer: outlook.timezone=\"$TIMEZONE\"" | jq '.value | to_entries | .[] | {n: (.key + 1), subject: .value.subject, start: .value.start.dateTime[0:16], end: .value.end.dateTime[0:16], location: (.value.location.displayName // ""), id: .value.id[-20:]}'
        ;;
    
    today)
        # List today's events
        TODAY_START=$(date -u +"%Y-%m-%dT00:00:00Z")
        TODAY_END=$(date -u +"%Y-%m-%dT23:59:59Z")
        curl -s "$API/calendarView?startDateTime=$TODAY_START&endDateTime=$TODAY_END&\$orderby=start/dateTime&\$select=id,subject,start,end,location" \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Prefer: outlook.timezone=\"$TIMEZONE\"" | jq 'if .value then (.value | to_entries | .[] | {n: (.key + 1), subject: .value.subject, start: .value.start.dateTime[0:16], end: .value.end.dateTime[0:16], location: (.value.location.displayName // ""), id: .value.id[-20:]}) else {error: .error.message} end'
        ;;
    
    week)
        # List this week's events
        WEEK_START=$(date -u +"%Y-%m-%dT00:00:00Z")
        WEEK_END=$(date -u -d "+7 days" +"%Y-%m-%dT23:59:59Z" 2>/dev/null || date -u -v+7d +"%Y-%m-%dT23:59:59Z")
        curl -s "$API/calendarView?startDateTime=$WEEK_START&endDateTime=$WEEK_END&\$orderby=start/dateTime&\$select=id,subject,start,end,location,isAllDay" \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Prefer: outlook.timezone=\"$TIMEZONE\"" | jq 'if .value then (.value | to_entries | .[] | {n: (.key + 1), subject: .value.subject, start: .value.start.dateTime[0:16], end: .value.end.dateTime[0:16], location: (.value.location.displayName // ""), id: .value.id[-20:]}) else {error: .error.message} end'
        ;;
    
    read)
        # Read event details
        EVENT_ID="$2"
        FULL_ID=$(find_event_id "$EVENT_ID")
        
        if [ -z "$FULL_ID" ]; then
            echo "Event not found"
            exit 1
        fi
        
        curl -s "$API/$CAL_PATH/events/$FULL_ID" \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Prefer: outlook.timezone=\"$TIMEZONE\"" | jq '{
                subject,
                start: .start.dateTime,
                end: .end.dateTime,
                location: .location.displayName,
                body: (if .body.contentType == "html" then (.body.content | gsub("<[^>]*>"; "") | gsub("\\s+"; " ")[0:500]) else .body.content[0:500] end),
                attendees: [.attendees[]?.emailAddress.address],
                isOnline: .isOnlineMeeting,
                link: .onlineMeeting.joinUrl
            }'
        ;;
    
    create)
        # Create event: outlook-calendar.sh create "Subject" "start" "end" [--location LOC] [--body TEXT] [--attendees a@b,c@d]
        shift
        SUBJECT="" ; START="" ; END="" ; LOCATION="" ; BODY_TEXT="" ; ATTENDEES=""
        while [ $# -gt 0 ]; do
            case "$1" in
                --location) LOCATION="$2"; shift 2;;
                --body) BODY_TEXT="$2"; shift 2;;
                --attendees) ATTENDEES="$2"; shift 2;;
                *)
                    if [ -z "$SUBJECT" ]; then SUBJECT="$1"
                    elif [ -z "$START" ]; then START="$1"
                    elif [ -z "$END" ]; then END="$1"
                    elif [ -z "$LOCATION" ]; then LOCATION="$1"
                    fi
                    shift;;
            esac
        done

        if [ -z "$SUBJECT" ] || [ -z "$START" ] || [ -z "$END" ]; then
            echo "Usage: outlook-calendar.sh create <subject> <start> <end> [location]"
            echo "       [--location LOC] [--body TEXT] [--attendees a@b.com,c@d.com]"
            echo "Date format: YYYY-MM-DDTHH:MM (e.g., 2026-01-26T10:00)"
            exit 1
        fi

        # Build JSON safely using jq to escape user input
        PAYLOAD=$(jq -n \
            --arg subject "$SUBJECT" \
            --arg start "$START" \
            --arg end "$END" \
            --arg tz "$TIMEZONE" \
            '{subject: $subject, start: {dateTime: $start, timeZone: $tz}, end: {dateTime: $end, timeZone: $tz}}')

        [ -n "$LOCATION" ] && PAYLOAD=$(echo "$PAYLOAD" | jq --arg v "$LOCATION" '. + {location: {displayName: $v}}')
        [ -n "$BODY_TEXT" ] && PAYLOAD=$(echo "$PAYLOAD" | jq --arg v "$BODY_TEXT" '. + {body: {contentType: "text", content: $v}}')
        if [ -n "$ATTENDEES" ]; then
            PAYLOAD=$(echo "$PAYLOAD" | jq --arg emails "$ATTENDEES" '. + {attendees: [$emails | split(",") | .[] | {emailAddress: {address: .}, type: "required"}]}')
        fi

        curl -s -X POST "$API/$CAL_PATH/events" \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$PAYLOAD" | jq '{status: "event created", subject: .subject, start: .start.dateTime[0:16], end: .end.dateTime[0:16], attendees: [.attendees[]?.emailAddress.address], id: .id[-20:]}'
        ;;
    
    quick)
        # Quick event (1 hour from now or specified time)
        SUBJECT="$2"
        START_TIME="${3:-}"
        
        if [ -z "$SUBJECT" ]; then
            echo "Usage: outlook-calendar.sh quick <subject> [start-time]"
            echo "If no time given, creates 1-hour event starting now"
            exit 1
        fi
        
        if [ -z "$START_TIME" ]; then
            START=$(date +"%Y-%m-%dT%H:%M")
            END=$(date -d "+1 hour" +"%Y-%m-%dT%H:%M" 2>/dev/null || date -v+1H +"%Y-%m-%dT%H:%M")
        else
            START="$START_TIME"
            # Parse and add 1 hour
            END=$(date -d "$START_TIME + 1 hour" +"%Y-%m-%dT%H:%M" 2>/dev/null || echo "$START_TIME")
        fi
        
        # Build JSON safely using jq to escape user input
        PAYLOAD=$(jq -n \
            --arg subject "$SUBJECT" \
            --arg start "$START" \
            --arg end "$END" \
            --arg tz "$TIMEZONE" \
            '{subject: $subject, start: {dateTime: $start, timeZone: $tz}, end: {dateTime: $end, timeZone: $tz}}')
        
        curl -s -X POST "$API/$CAL_PATH/events" \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$PAYLOAD" | jq '{status: "quick event created", subject: .subject, start: .start.dateTime[0:16], end: .end.dateTime[0:16], id: .id[-20:]}'
        ;;
    
    delete)
        # Delete event
        EVENT_ID="$2"
        FULL_ID=$(find_event_id "$EVENT_ID")
        
        if [ -z "$FULL_ID" ]; then
            echo "Event not found"
            exit 1
        fi
        
        RESULT=$(curl -s -w "\n%{http_code}" -X DELETE "$API/$CAL_PATH/events/$FULL_ID" \
            -H "Authorization: Bearer $ACCESS_TOKEN")
        
        HTTP_CODE=$(echo "$RESULT" | tail -1)
        if [ "$HTTP_CODE" = "204" ]; then
            jq -n --arg id "$EVENT_ID" '{status: "event deleted", id: $id}'
        else
            echo "$RESULT" | head -n -1 | jq '.error // .'
        fi
        ;;
    
    update)
        # Update event: outlook-calendar.sh update <id> <field> <value>
        # Or multi-field: outlook-calendar.sh update <id> --body TEXT --attendees a@b,c@d
        EVENT_ID="$2"
        shift 2

        if [ -z "$EVENT_ID" ] || [ $# -eq 0 ]; then
            echo "Usage: outlook-calendar.sh update <id> <field> <value>"
            echo "       outlook-calendar.sh update <id> [--subject V] [--location V] [--start V] [--end V] [--body V] [--attendees a@b,c@d] [--add-attendees a@b,c@d]"
            echo "Fields: subject, location, start, end, body, attendees"
            exit 1
        fi

        FULL_ID=$(find_event_id "$EVENT_ID")

        if [ -z "$FULL_ID" ]; then
            echo "Event not found"
            exit 1
        fi

        # Support both old-style (update <id> <field> <value>) and new-style (update <id> --field value)
        PATCH='{}'
        ADD_ATTENDEES=""
        if [ "${1:0:2}" != "--" ]; then
            # Old style: update <id> <field> <value>
            FIELD="$1"; VALUE="$2"
            case "$FIELD" in
                subject) PATCH=$(jq -n --arg v "$VALUE" '{subject: $v}');;
                location) PATCH=$(jq -n --arg v "$VALUE" '{location: {displayName: $v}}');;
                start) PATCH=$(jq -n --arg v "$VALUE" --arg tz "$TIMEZONE" '{start: {dateTime: $v, timeZone: $tz}}');;
                end) PATCH=$(jq -n --arg v "$VALUE" --arg tz "$TIMEZONE" '{end: {dateTime: $v, timeZone: $tz}}');;
                body) PATCH=$(jq -n --arg v "$VALUE" '{body: {contentType: "text", content: $v}}');;
                attendees) PATCH=$(jq -n --arg emails "$VALUE" '{attendees: [$emails | split(",") | .[] | {emailAddress: {address: .}, type: "required"}]}');;
                *) echo "Unknown field: $FIELD"; exit 1;;
            esac
        else
            # New style: update <id> --field value --field value
            while [ $# -gt 0 ]; do
                case "$1" in
                    --subject) PATCH=$(echo "$PATCH" | jq --arg v "$2" '. + {subject: $v}'); shift 2;;
                    --location) PATCH=$(echo "$PATCH" | jq --arg v "$2" '. + {location: {displayName: $v}}'); shift 2;;
                    --start) PATCH=$(echo "$PATCH" | jq --arg v "$2" --arg tz "$TIMEZONE" '. + {start: {dateTime: $v, timeZone: $tz}}'); shift 2;;
                    --end) PATCH=$(echo "$PATCH" | jq --arg v "$2" --arg tz "$TIMEZONE" '. + {end: {dateTime: $v, timeZone: $tz}}'); shift 2;;
                    --body) PATCH=$(echo "$PATCH" | jq --arg v "$2" '. + {body: {contentType: "text", content: $v}}'); shift 2;;
                    --attendees) PATCH=$(echo "$PATCH" | jq --arg emails "$2" '. + {attendees: [$emails | split(",") | .[] | {emailAddress: {address: .}, type: "required"}]}'); shift 2;;
                    --add-attendees) ADD_ATTENDEES="$2"; shift 2;;
                    *) echo "Unknown flag: $1"; exit 1;;
                esac
            done
        fi

        # Handle --add-attendees by fetching existing attendees first
        if [ -n "$ADD_ATTENDEES" ]; then
            EXISTING=$(curl -s "$API/$CAL_PATH/events/$FULL_ID?\$select=attendees" \
                -H "Authorization: Bearer $ACCESS_TOKEN" | jq '.attendees // []')
            NEW=$(jq -n --arg emails "$ADD_ATTENDEES" '[$emails | split(",") | .[] | {emailAddress: {address: .}, type: "required"}]')
            MERGED=$(jq -n --argjson e "$EXISTING" --argjson n "$NEW" '$e + $n')
            PATCH=$(echo "$PATCH" | jq --argjson a "$MERGED" '. + {attendees: $a}')
        fi

        curl -s -X PATCH "$API/$CAL_PATH/events/$FULL_ID" \
            -H "Authorization: Bearer $ACCESS_TOKEN" \
            -H "Content-Type: application/json" \
            -d "$PATCH" | jq '{status: "event updated", subject: .subject, start: .start.dateTime[0:16], end: .end.dateTime[0:16], attendees: [.attendees[]?.emailAddress.address], id: .id[-20:]}'
        ;;
    
    calendars)
        # List all calendars
        curl -s "$API/calendars" \
            -H "Authorization: Bearer $ACCESS_TOKEN" | jq '.value[] | {name: .name, color: .color, canEdit: .canEdit, id: .id[-20:]}'
        ;;
    
    free)
        # Check free/busy for a time range
        START="$2"
        END="$3"
        
        if [ -z "$START" ] || [ -z "$END" ]; then
            echo "Usage: outlook-calendar.sh free <start> <end>"
            echo "Date format: YYYY-MM-DDTHH:MM"
            exit 1
        fi
        
        curl -s "$API/calendarView?startDateTime=${START}:00Z&endDateTime=${END}:00Z&\$select=subject,start,end" \
            -H "Authorization: Bearer $ACCESS_TOKEN" | jq --arg start "$START" --arg end "$END" 'if (.value | length) == 0 then {status: "free", start: $start, end: $end} else {status: "busy", events: [.value[].subject]} end'
        ;;
    
    *)
        echo "Usage: outlook-calendar.sh <command> [args]"
        echo ""
        echo "VIEW:"
        echo "  events [count]            - List upcoming events"
        echo "  today                     - Today's events"
        echo "  week                      - This week's events"
        echo "  read <id>                 - Event details"
        echo "  calendars                 - List all calendars"
        echo "  free <start> <end>        - Check availability"
        echo ""
        echo "CREATE:"
        echo "  create <subj> <start> <end> [loc] [--body TEXT] [--attendees a@b,c@d]"
        echo "  quick <subject> [time]    - Quick 1-hour event"
        echo ""
        echo "MANAGE:"
        echo "  update <id> <field> <val> - Update single field"
        echo "  update <id> --body TEXT --attendees a@b,c@d --add-attendees x@y"
        echo "  delete <id>               - Delete event"
        echo ""
        echo "Date format: YYYY-MM-DDTHH:MM (e.g., 2026-01-26T10:00)"
        ;;
esac
