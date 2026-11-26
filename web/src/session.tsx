import { createContext, useContext } from "react";

export interface User {
  id: number;
  username: string;
  full_name: string;
  role: string;
  permissions: string[];
  must_change_password: boolean;
}
export interface Session {
  user: User;
  csrf: string;
  version: string;
  idle_timeout: number;
  logout: () => void;
  can: (perm: string) => boolean;
  hostname: string;
}
export const SessionCtx = createContext<Session>(null as any);
export const useSession = () => useContext(SessionCtx);
