import { ApiClient } from "../api/client";
import { Note, parseNote, serializeNote } from "../models/note";

export class NotesStore {
  notes: Note[] = [];

  constructor(private api: ApiClient) {}

  /** Fetch all notes from the server. */
  async load(): Promise<void> {
    const data = await this.api.get<any[]>("/notes");
    this.notes = data.map(parseNote);
  }

  /** Persist one note to the server. */
  async save(note: Note): Promise<void> {
    await this.api.post("/notes", serializeNote(note));
  }
}
