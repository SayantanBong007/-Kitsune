export const formatDate = (d: Date): string => d.toISOString().slice(0, 10);

export function slugify(s: string): string {
  return s.toLowerCase().replace(/[^a-z0-9]+/g, "-");
}
