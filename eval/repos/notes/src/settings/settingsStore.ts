export class SettingsStore {
  theme = "light";

  save(): void {
    localStorage.setItem("settings", JSON.stringify({ theme: this.theme }));
  }

  load(): void {
    const raw = localStorage.getItem("settings");
    if (raw) this.theme = JSON.parse(raw).theme;
  }
}
