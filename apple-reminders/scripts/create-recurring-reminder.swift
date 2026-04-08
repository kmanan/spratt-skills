import EventKit
import Foundation

let store = EKEventStore()
let semaphore = DispatchSemaphore(value: 0)

// Parse command line args
let args = CommandLine.arguments
guard args.count >= 4 else {
    fputs("Usage: swift create_recurring_reminder.swift <title> <day-of-week> <HH:MM> [list-name] [notes]\n", stderr)
    fputs("  day-of-week: monday, tuesday, ... sunday\n", stderr)
    fputs("  Example: swift create_recurring_reminder.swift \"Buy milk\" monday 07:30\n", stderr)
    exit(1)
}

let title = args[1]
let dayStr = args[2].lowercased()
let timeParts = args[3].split(separator: ":").compactMap { Int($0) }
guard timeParts.count == 2 else {
    fputs("Error: time must be HH:MM format\n", stderr)
    exit(1)
}
let hour = timeParts[0]
let minute = timeParts[1]
let listName = args.count > 4 ? args[4] : ""
let notes = args.count > 5 ? args[5] : ""

let dayMap: [String: EKWeekday] = [
    "sunday": .sunday, "monday": .monday, "tuesday": .tuesday,
    "wednesday": .wednesday, "thursday": .thursday, "friday": .friday,
    "saturday": .saturday
]
guard let weekday = dayMap[dayStr] else {
    fputs("Error: invalid day '\(dayStr)'. Use: monday, tuesday, etc.\n", stderr)
    exit(1)
}

store.requestFullAccessToReminders { granted, error in
    guard granted else {
        fputs("Error: Reminders access denied: \(error?.localizedDescription ?? "unknown")\n", stderr)
        exit(1)
    }
    
    let reminder = EKReminder(eventStore: store)
    reminder.title = title
    
    // Find the target list if specified
    if !listName.isEmpty {
        let calendars = store.calendars(for: .reminder)
        if let cal = calendars.first(where: { $0.title.lowercased() == listName.lowercased() }) {
            reminder.calendar = cal
        } else {
            fputs("Warning: list '\(listName)' not found, using default\n", stderr)
            reminder.calendar = store.defaultCalendarForNewReminders()
        }
    } else {
        reminder.calendar = store.defaultCalendarForNewReminders()
    }
    
    // Calculate next occurrence of the target day
    let calendar = Calendar.current
    var components = DateComponents()
    components.hour = hour
    components.minute = minute
    components.weekday = weekday.rawValue  // EKWeekday raw values match Calendar weekday
    
    let now = Date()
    guard let nextDate = calendar.nextDate(after: now, matching: components, matchingPolicy: .nextTime) else {
        fputs("Error: could not calculate next date\n", stderr)
        exit(1)
    }
    
    reminder.dueDateComponents = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: nextDate)
    
    // Add alarm at due time
    reminder.addAlarm(EKAlarm(absoluteDate: nextDate))
    
    // Set notes if provided
    if !notes.isEmpty {
        reminder.notes = notes
    }
    
    // Set weekly recurrence
    let rule = EKRecurrenceRule(
        recurrenceWith: .weekly,
        interval: 1,
        daysOfTheWeek: [EKRecurrenceDayOfWeek(weekday)],
        daysOfTheMonth: nil,
        monthsOfTheYear: nil,
        weeksOfTheYear: nil,
        daysOfTheYear: nil,
        setPositions: nil,
        end: nil
    )
    reminder.addRecurrenceRule(rule)
    
    do {
        try store.save(reminder, commit: true)
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "yyyy-MM-dd HH:mm"
        print("OK: Created recurring reminder")
        print("  Title: \(title)")
        print("  List: \(reminder.calendar.title)")
        print("  Next due: \(dateFormatter.string(from: nextDate))")
        print("  Recurrence: weekly on \(dayStr)")
        print("  ID: \(reminder.calendarItemIdentifier)")
    } catch {
        fputs("Error saving reminder: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
    
    semaphore.signal()
}

semaphore.wait()
