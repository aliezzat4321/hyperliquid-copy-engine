export function scheduleDeferredPersistence(
  persist: () => void,
  onError: (error: unknown) => void,
): void {
  setImmediate(() => {
    try { persist(); } catch (error) { onError(error); }
  });
}
