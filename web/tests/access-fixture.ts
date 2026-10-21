import type { Access } from "../src/api";

export const privateAccess: Access = {
  mode: "private",
  principal: { kind: "private", id: null },
  can_search: true,
  login_url: null,
  logout_url: null,
  limits: { turns: null, window_seconds: null },
};

export const guestAccess: Access = {
  mode: "mixed",
  principal: { kind: "guest", id: "a".repeat(64) },
  can_search: true,
  login_url: "/auth/login",
  logout_url: null,
  limits: { turns: 3, window_seconds: 86400 },
};

export const userAccess: Access = {
  mode: "mixed",
  principal: { kind: "user", id: "b".repeat(64) },
  can_search: true,
  login_url: null,
  logout_url: "/auth/logout",
  limits: { turns: 30, window_seconds: 86400 },
};
