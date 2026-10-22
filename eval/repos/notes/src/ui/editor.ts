import { NotesStore } from "../store/notesStore";
import { Note } from "../models/note";
import { formatDate } from "../utils/format";

function debounce(fn: () => void, ms: number): () => void {
  let timer: any;
  return () => {
    clearTimeout(timer);
    timer = setTimeout(fn, ms);
  };
}

/** Save the note being edited at most every 500 ms while the user types. */
export function autosave(store: NotesStore, current: () => Note): () => void {
  return debounce(() => store.save(current()), 500);
}

export const renderEditor = (store: NotesStore, note: Note): HTMLElement => {
  const el = document.createElement("div");
  el.textContent = `${note.title} — ${formatDate(note.updatedAt)}`;
  const button = document.createElement("button");
  button.onclick = () => store.save(note);
  el.appendChild(button);
  return el;
};
