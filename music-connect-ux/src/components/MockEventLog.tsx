export function MockEventLog({ events }: { events: string[] }) { return <div className="eventLog">{events.slice(-6).map((e, i) => <div key={`${e}-${i}`}>{e}</div>)}</div>; }
