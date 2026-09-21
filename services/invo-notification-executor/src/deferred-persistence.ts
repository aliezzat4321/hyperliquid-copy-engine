export function scheduleDeferredPersistence(
  persist: () => void,
  onError: (error: unknown) => void,
): void {
  setImmediate(() => {
    try {
      persist();
    } catch (error) {
      try {
        onError(error);
      } catch {
        // Discovery-side persistence/reporting is non-critical. Never let a
        // secondary error handler kill CLOSE/gap reconciliation.
      }
    }
  });
}
