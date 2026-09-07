import { useMemo } from "react";
import * as platform from "../utils/platform";

// Fetch options carrying the local API token, for a caller that fetches a
// protected media/file endpoint directly instead of going through api/client.
export default function useApiFetchParams() {
  const token = platform.apiToken();
  return useMemo(() => (token ? { headers: { "X-ADVoice-Token": token } } : undefined), [token]);
}
