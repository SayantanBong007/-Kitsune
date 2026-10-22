export interface Note {
  id: string;
  title: string;
  body: string;
  updatedAt: Date;
}

export function parseNote(raw: any): Note {
  return { id: raw.id, title: raw.title, body: raw.body, updatedAt: new Date(raw.updated_at) };
}

export function serializeNote(note: Note): Record<string, string> {
  return { id: note.id, title: note.title, body: note.body, updated_at: note.updatedAt.toISOString() };
}
