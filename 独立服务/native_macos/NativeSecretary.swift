import Cocoa
import EventKit
import CryptoKit
import Darwin

final class SecretaryApp: NSObject, NSApplicationDelegate {
    let store = EKEventStore()
    func applicationDidFinishLaunching(_ notification: Notification) {
        Task { @MainActor in
            do {
                guard CommandLine.arguments.count == 2 else { throw NSError(domain:"request_path_required",code:1) }
                let path = URL(fileURLWithPath:CommandLine.arguments[1]).standardizedFileURL
                let request = try JSONSerialization.jsonObject(with:Data(contentsOf:path)) as! [String:Any]
                let output = try await self.run(request)
                try self.write(output, path.deletingPathExtension().appendingPathExtension("result.json"))
            } catch {
                if CommandLine.arguments.count == 2 {
                    let path=URL(fileURLWithPath:CommandLine.arguments[1])
                    try? self.write(["ok":false,"error":(error as NSError).domain],path.deletingPathExtension().appendingPathExtension("result.json"))
                }
            }
            NSApp.terminate(nil)
        }
    }
    func write(_ value:[String:Any],_ path:URL) throws {
        try JSONSerialization.data(withJSONObject:value,options:[.sortedKeys]).write(to:path,options:.atomic)
        try FileManager.default.setAttributes([.posixPermissions:0o600],ofItemAtPath:path.path)
    }
    func run(_ request:[String:Any]) async throws -> [String:Any] {
        let command=request["command"] as? String ?? "status"
        if command == "execute" || command == "reconcile" {
            let descriptor=try nativeLock()
            defer { flock(descriptor,LOCK_UN);Darwin.close(descriptor) }
            do { return try await execute(request, command == "reconcile") }
            catch {
                var output:[String:Any] = ["ok":false,"error":(error as NSError).domain]
                if let envelope=request["envelope"] as? [String:Any],let op=envelope["operation_id"] as? String,
                   var data=try? ledger(),var operations=data["operations"] as? [String:[String:Any]] {
                    let state=operations[op]?["state"] as? String
                    if state == nil || state == "prepared" || state == "not_started" {
                        operations[op] = ["state":"not_started","scope":request["scope"] ?? "","action":envelope["action"] ?? [:],"booking":envelope["booking"] ?? [:]]
                        data["operations"]=operations
                        if (try? persist(data)) != nil { output["not_started"]=true }
                    }
                }
                return output
            }
        }
        if command == "inspect" {
            let descriptor=try nativeLock()
            defer { flock(descriptor,LOCK_UN);Darwin.close(descriptor) }
            return try inspect(request)
        }
        if command == "authorize" {
            _ = try await store.requestFullAccessToReminders()
            _ = try await store.requestFullAccessToEvents()
        }
        let reminders=EKEventStore.authorizationStatus(for:.reminder) == .fullAccess
        let events=EKEventStore.authorizationStatus(for:.event) == .fullAccess
        let sources=store.sources.filter{$0.sourceType == .calDAV && $0.title.lowercased().contains("icloud")}
        return ["ok":true,"reminders_authorized":reminders,"events_authorized":events,
                "icloud_sources":sources.map{["id":$0.sourceIdentifier,"name":$0.title]},
                "sources_ready":try sourceReady("reminder") && sourceReady("event"),
                "reminder_connection":try connectionDetails("reminder"),"event_connection":try connectionDetails("event"),
                "phone_notification_verified":false]
    }
}

extension SecretaryApp {
    var storage: URL { Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("features/native") }
    func nativeLock() throws -> Int32 {
        try FileManager.default.createDirectory(at:storage,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
        let path=storage.appendingPathComponent("ledger.lock").path
        let descriptor=Darwin.open(path,O_CREAT|O_RDWR,0o600)
        guard descriptor>=0 else { throw failure("native_lock_unavailable") }
        if flock(descriptor,LOCK_EX|LOCK_NB) != 0 { Darwin.close(descriptor);throw failure("native_busy") }
        return descriptor
    }
    func failure(_ code:String) -> NSError { NSError(domain:code,code:1) }
    func empty(_ value:Any?) -> Bool { value == nil || value is NSNull }
    func parseDate(_ raw:Any?) throws -> Date? {
        if empty(raw) { return nil }
        guard let text=raw as? String else { throw failure("invalid_date") }
        let parser=ISO8601DateFormatter()
        if let date=parser.date(from:text) { return date }
        parser.formatOptions=[.withInternetDateTime,.withFractionalSeconds]
        guard let date=parser.date(from:text) else { throw failure("invalid_date") };return date
    }
    func same(_ a:Any,_ b:Any) -> Bool {
        guard let x=try? JSONSerialization.data(withJSONObject:a,options:[.sortedKeys,.fragmentsAllowed]),let y=try? JSONSerialization.data(withJSONObject:b,options:[.sortedKeys,.fragmentsAllowed]) else { return false };return x == y
    }
    func ledger() throws -> [String:Any] {
        try FileManager.default.createDirectory(at:storage,withIntermediateDirectories:true,attributes:[.posixPermissions:0o700])
        let path=storage.appendingPathComponent("ledger.json")
        if !FileManager.default.fileExists(atPath:path.path) { return ["operations":[String:Any](),"items":[String:Any](),"calendars":[String:String]()] }
        return try JSONSerialization.jsonObject(with:Data(contentsOf:path)) as! [String:Any]
    }
    func persist(_ ledger:[String:Any]) throws { try write(ledger,storage.appendingPathComponent("ledger.json")) }
    func sourceCandidates(_ kind:String) -> [EKSource] {
        let entity:EKEntityType = kind == "reminder" ? .reminder:.event
        let sources=store.sources.filter{$0.sourceType == .calDAV && $0.title.lowercased().contains("icloud")}
        let actual=Set(store.calendars(for:entity).map{$0.source.sourceIdentifier})
        let candidates=sources.filter{actual.contains($0.sourceIdentifier)}
        return candidates.isEmpty && sources.count == 1 ? sources:candidates
    }
    func matchingCalendars(_ kind:String) -> [EKCalendar] {
        let entity:EKEntityType = kind == "reminder" ? .reminder:.event
        let title=kind == "reminder" ? "言小办商用提醒":"言小办商用日程"
        return store.calendars(for:entity).filter{$0.title==title && $0.allowsContentModifications && $0.source.sourceType == .calDAV && $0.source.title.lowercased().contains("icloud")}
    }
    func sourceReady(_ kind:String) throws -> Bool {
        let data=try ledger();let calendars=data["calendars"] as? [String:String] ?? [:]
        if let identifier=calendars[kind] {
            let entity:EKEntityType = kind == "reminder" ? .reminder:.event
            if store.calendars(for:entity).contains(where:{$0.calendarIdentifier==identifier && $0.allowsContentModifications && $0.source.sourceType == .calDAV && $0.source.title.lowercased().contains("icloud")}) { return true }
            return matchingCalendars(kind).count == 1
        }
        let matches=matchingCalendars(kind)
        return matches.count == 1 || (matches.isEmpty && sourceCandidates(kind).count == 1)
    }
    func connectionDetails(_ kind:String) throws -> [String:Any] {
        let data=try ledger();let calendars=data["calendars"] as? [String:String] ?? [:]
        let entity:EKEntityType = kind == "reminder" ? .reminder:.event
        return ["registered_present":calendars[kind].map{id in store.calendars(for:entity).contains{$0.calendarIdentifier==id}} ?? false,"same_named_count":matchingCalendars(kind).count,"source_candidates":sourceCandidates(kind).count]
    }
    func calendar(_ kind:String,_ ledger:inout [String:Any]) throws -> EKCalendar {
        var calendars=ledger["calendars"] as? [String:String] ?? [:]
        if let id=calendars[kind] {
            let entity:EKEntityType = kind == "reminder" ? .reminder:.event
            if let value=store.calendars(for:entity).first(where:{$0.calendarIdentifier==id}),value.allowsContentModifications { return value }
            let matches=matchingCalendars(kind)
            guard matches.count == 1 else { throw failure("registered_calendar_unavailable") }
            calendars[kind]=matches[0].calendarIdentifier;ledger["calendars"]=calendars;try persist(ledger);return matches[0]
        }
        let matches=matchingCalendars(kind)
        if matches.count == 1 { calendars[kind]=matches[0].calendarIdentifier;ledger["calendars"]=calendars;try persist(ledger);return matches[0] }
        if !matches.isEmpty { throw failure("registered_calendar_ambiguous") }
        let sources=sourceCandidates(kind)
        guard sources.count == 1 else { throw failure("icloud_source_not_unique_or_disabled") }
        let value=EKCalendar(for:kind == "reminder" ? .reminder:.event,eventStore:store)
        value.source=sources[0];value.title=kind == "reminder" ? "言小办商用提醒":"言小办商用日程"
        try store.saveCalendar(value,commit:true);calendars[kind]=value.calendarIdentifier;ledger["calendars"]=calendars;try persist(ledger);return value
    }
    func renderedNotes(_ fields:[String:Any],_ booking:[String:Any]) throws -> String {
        var text=fields["notes"] as? String ?? ""
        if let state=booking["state"] as? String,state != "not_applicable" {
            let names=["purchased":"用户自述已购票（未经订单核验）","not_purchased":"用户自述未购票 · 暂定安排","unknown":"购票状态未知 · 暂定安排"]
            text += (text.isEmpty ? "":"\n\n")+(names[state] ?? "购票状态未知")
            if let evidence=booking["evidence"] as? String { text += "\n用户原话："+evidence }
        }
        return text
    }
    func apply(_ fields:[String:Any],_ booking:[String:Any],_ item:EKCalendarItem,_ marker:String) throws {
        guard let title=fields["title"] as? String,!title.isEmpty else { throw failure("invalid_title") }
        item.title=title;item.notes=try renderedNotes(fields,booking);item.url=URL(string:marker)
        item.alarms=[]
        if let alert=try parseDate(fields["remind_at"]) { item.addAlarm(EKAlarm(absoluteDate:alert)) }
        if let event=item as? EKEvent {
            guard let start=try parseDate(fields["start_at"]),let end=try parseDate(fields["end_at"]),end>start else { throw failure("event_end_required") }
            event.startDate=start;event.endDate=end;event.timeZone=TimeZone(identifier:"Asia/Shanghai");event.location=fields["location"] as? String
        } else if let reminder=item as? EKReminder {
            if let date=try parseDate(fields["remind_at"]) {
                var cal=Calendar(identifier:.gregorian);cal.timeZone=TimeZone(identifier:"Asia/Shanghai")!
                var parts=cal.dateComponents([.year,.month,.day,.hour,.minute,.second],from:date);parts.timeZone=cal.timeZone;parts.calendar=cal;reminder.startDateComponents=parts;reminder.dueDateComponents=parts
            } else { reminder.startDateComponents=nil;reminder.dueDateComponents=nil }
        }
    }
    func verify(_ item:EKCalendarItem,_ fields:[String:Any],_ booking:[String:Any],_ marker:String,requireIOSStart:Bool = true) throws {
        guard item.title == fields["title"] as? String,(item.notes ?? "") == (try renderedNotes(fields,booking)),item.url?.absoluteString == marker else { throw failure("native_readback_mismatch") }
        let expected=try parseDate(fields["remind_at"]);let alarms=item.alarms ?? []
        if let date=expected { guard alarms.count == 1,let actual=alarms[0].absoluteDate,abs(actual.timeIntervalSince(date))<0.5 else { throw failure("native_alarm_mismatch") } }
        else if !alarms.isEmpty { throw failure("native_alarm_mismatch") }
        if let event=item as? EKEvent {
            guard let start=try parseDate(fields["start_at"]),let end=try parseDate(fields["end_at"]),abs(event.startDate.timeIntervalSince(start))<0.5,abs(event.endDate.timeIntervalSince(end))<0.5,event.location ?? "" == fields["location"] as? String ?? "" else { throw failure("native_event_mismatch") }
        } else if let reminder=item as? EKReminder {
            if let expected=expected { var calendar=Calendar(identifier:.gregorian);calendar.timeZone=TimeZone(identifier:"Asia/Shanghai")!;guard let components=reminder.dueDateComponents,let due=calendar.date(from:components),abs(due.timeIntervalSince(expected))<0.5 else { throw failure("native_due_mismatch") };if requireIOSStart { guard let parts=reminder.startDateComponents,parts.hour != nil,parts.minute != nil,let start=calendar.date(from:parts),abs(start.timeIntervalSince(expected))<0.5 else { throw failure("ios_reminder_start_missing_or_mismatched") } } }
            else if reminder.dueDateComponents != nil || (requireIOSStart && reminder.startDateComponents != nil) { throw failure("native_due_mismatch") }
        }
    }
    func findMarker(_ marker:String,_ kind:String,_ calendar:EKCalendar,_ fields:[String:Any]) async throws -> EKCalendarItem? {
        if kind == "reminder" {
            let predicate=store.predicateForReminders(in:[calendar])
            let values: [EKReminder] = await withCheckedContinuation { continuation in store.fetchReminders(matching:predicate){continuation.resume(returning:$0 ?? [])} }
            let matches=values.filter{$0.url?.absoluteString == marker};guard matches.count<=1 else { throw failure("duplicate_operation_marker") };return matches.first
        }
        guard let date=try parseDate(fields["start_at"]) else { return nil }
        let predicate=store.predicateForEvents(withStart:date.addingTimeInterval(-86400),end:date.addingTimeInterval(86400),calendars:[calendar])
        let matches=store.events(matching:predicate).filter{$0.url?.absoluteString == marker};guard matches.count<=1 else { throw failure("duplicate_operation_marker") };return matches.first
    }
    func components(_ value:DateComponents?) -> Any {
        guard let value=value else { return NSNull() }
        return ["year":value.year as Any? ?? NSNull(),"month":value.month as Any? ?? NSNull(),"day":value.day as Any? ?? NSNull(),"hour":value.hour as Any? ?? NSNull(),"minute":value.minute as Any? ?? NSNull(),"second":value.second as Any? ?? NSNull(),"timezone":value.timeZone?.identifier as Any? ?? NSNull()]
    }
    func inspect(_ request:[String:Any]) throws -> [String:Any] {
        guard let scope=request["scope"] as? String,let identifier=request["native_id"] as? String else { throw failure("invalid_inspect_request") }
        let data=try ledger();let items=data["items"] as! [String:[String:Any]]
        guard let owned=items[identifier],owned["scope"] as? String == scope,owned["deleted"] as? Bool == false else { throw failure("target_not_owned") }
        store.reset();guard let item=store.calendarItem(withIdentifier:identifier) else { return ["ok":true,"exists":false] }
        var result:[String:Any] = ["ok":true,"exists":true,"alarms":(item.alarms ?? []).map{["absolute":$0.absoluteDate.map{ISO8601DateFormatter().string(from:$0)} as Any? ?? NSNull(),"relative":$0.relativeOffset]}]
        if let reminder=item as? EKReminder { result["start"]=components(reminder.startDateComponents);result["due"]=components(reminder.dueDateComponents);result["completed"]=reminder.isCompleted }
        return result
    }
    func execute(_ request:[String:Any],_ reconcileOnly:Bool) async throws -> [String:Any] {
        guard let scope=request["scope"] as? String,scope.range(of:"^[a-f0-9]{32}$",options:.regularExpression) != nil,
              let envelope=request["envelope"] as? [String:Any],let op=envelope["operation_id"] as? String,
              let action=envelope["action"] as? [String:Any],let tool=action["tool"] as? String,
              let booking=envelope["booking"] as? [String:Any],let actionHash=envelope["action_sha256"] as? String else { throw failure("invalid_native_request") }
        guard ["create_reminder","create_event","update_item","delete_item"].contains(tool) else { throw failure("unsupported_native_action") }
        var data=try ledger();var operations=data["operations"] as! [String:[String:Any]];var items=data["items"] as! [String:[String:Any]]
        let creating=tool.hasPrefix("create_");var entry=operations[op]
        if let prior=entry {
            guard prior["scope"] as? String == scope,same(prior["action"]!,action),same(prior["booking"]!,booking) else { throw failure("native_operation_conflict") }
        } else {
            if reconcileOnly { throw failure("operation_not_started") }
            var fields:[String:Any];var kind:String;var marker:String;var nativeID:String?
            if creating {
                kind=tool == "create_event" ? "event":"reminder"
                let names=kind == "event" ? ["title","start_at","end_at","remind_at","location","notes"]:["title","remind_at","notes"]
                fields=Dictionary(uniqueKeysWithValues:names.map{($0,action[$0] ?? NSNull())})
                marker="xiaopai-commercial://item/\(scope)/\(op)";nativeID=nil
            } else {
                guard let id=action["target_id"] as? String,let target=items[id],target["scope"] as? String == scope,target["deleted"] as? Bool == false else { throw failure("target_not_owned") }
                nativeID=id;kind=target["kind"] as! String;marker=target["marker"] as! String;fields=target["fields"] as! [String:Any]
                guard let native=store.calendarItem(withIdentifier:id) else { throw failure("target_unavailable") }
                try verify(native,fields,target["booking"] as! [String:Any],marker,requireIOSStart:false)
                if tool == "update_item" { guard let changes=action["changes"] as? [String:Any] else { throw failure("invalid_changes") };for(k,v) in changes { fields[k]=v } }
            }
            if kind == "event" && empty(fields["end_at"]) { throw failure("event_end_required") }
            entry=["scope":scope,"action":action,"booking":booking,"fields":fields,"kind":kind,"marker":marker,"native_id":nativeID as Any? ?? NSNull(),"state":"prepared"]
            operations[op]=entry!;data["operations"]=operations;try persist(data)
        }
        var record=entry!;let kind=record["kind"] as! String;let fields=record["fields"] as! [String:Any];let marker=record["marker"] as! String
        guard EKEventStore.authorizationStatus(for:kind == "reminder" ? .reminder:.event) == .fullAccess else { throw failure("native_permission_required") }
        let cal=try calendar(kind,&data)
        var nativeID=record["native_id"] as? String
        if record["state"] as? String == "prepared" && !reconcileOnly {
            record["state"]="started";operations[op]=record;data["operations"]=operations;try persist(data)
            var item:EKCalendarItem
            if creating { item=kind == "reminder" ? EKReminder(eventStore:store):EKEvent(eventStore:store);item.calendar=cal }
            else { guard let value=store.calendarItem(withIdentifier:nativeID!) else { throw failure("target_unavailable") };item=value }
            if tool == "delete_item" {
                if let reminder=item as? EKReminder { try store.remove(reminder,commit:true) }
                else { try store.remove(item as! EKEvent,span:.thisEvent,commit:true) }
            } else {
                try apply(fields,booking,item,marker)
                if let reminder=item as? EKReminder { try store.save(reminder,commit:true) }
                else { try store.save(item as! EKEvent,span:.thisEvent,commit:true) }
                nativeID=item.calendarItemIdentifier
            }
            record["native_id"]=nativeID!;operations[op]=record;data["operations"]=operations;try persist(data)
        }
        store.reset()
        if nativeID == nil && creating {
            if let refreshed=store.calendar(withIdentifier:cal.calendarIdentifier),let value=try await findMarker(marker,kind,refreshed,fields) { nativeID=value.calendarItemIdentifier;record["native_id"]=nativeID! }
        }
        guard let identifier=nativeID else { throw failure("native_outcome_unknown") }
        let deleted=tool == "delete_item"
        let native=store.calendarItem(withIdentifier:identifier)
        if deleted { guard native == nil else { throw failure("native_delete_unverified") } }
        else { guard let value=native else { throw failure("native_item_unavailable") };try verify(value,fields,booking,marker) }
        record["state"]="succeeded";operations[op]=record
        items[identifier]=["scope":scope,"kind":kind,"marker":marker,"fields":fields,"booking":booking,"deleted":deleted]
        data["operations"]=operations;data["items"]=items;try persist(data)
        return ["ok":true,"native_visible_notes":native?.notes as Any? ?? NSNull(),"receipt":["protocol_version":1,"operation_id":op,"action_sha256":actionHash,"outcome":"succeeded","native_id":identifier,
             "readback":["operation_id":op,"exists":!deleted,"fields":deleted ? NSNull():fields as Any,"booking":booking]]]
    }
}

let app=NSApplication.shared
let delegate=SecretaryApp()
app.delegate=delegate
app.setActivationPolicy(.accessory)
app.run()
