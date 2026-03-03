import { ReactRouter } from '@decky/ui';

// This implementation is taken from protondb-decky; it grabs the internal
// router hook from the Decky UI package via duck typing.  The hook simply
// returns whatever route parameters are currently in the URL.

const paramsHook = Object.values(ReactRouter).find((val) =>
  /return (\w)\?\1\.params:{}/.test(`${val}`)
);

export function useParams<T>(): T {
  if (typeof paramsHook === "function") {
    return (paramsHook as <K>() => K)<T>();
  }
  console.error("[ Decky Links ] Failed to resolve internal route params hook.");
  return {} as T;
}
