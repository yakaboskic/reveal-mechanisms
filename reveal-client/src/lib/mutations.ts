/** Reuse an uncertain write's key, then retire it as soon as its response confirms success. */
export function createMutationKeys(newKey = () => crypto.randomUUID()) {
  const keys = new Map<string, string>();
  return {
    async run<T>(intent: unknown, write: (key: string) => Promise<T>): Promise<T> {
      const fingerprint = JSON.stringify(intent);
      let key = keys.get(fingerprint);
      if (!key) { key = newKey(); keys.set(fingerprint, key); }
      const result = await write(key);
      keys.delete(fingerprint);
      return result;
    },
  };
}
