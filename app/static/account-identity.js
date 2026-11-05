/* Read only the signed-in account. A visited profile or note author is not identity. */
(async () => {
  const host = location.hostname;
  if (!["www.xiaohongshu.com", "xiaohongshu.com", "creator.xiaohongshu.com"].includes(host)) {
    return { logged_in: null, error: "当前页面不是小红书" };
  }
  const creator = host === "creator.xiaohongshu.com";
  const unwrap = (value) => value?._value ?? value?.value ?? value;
  const firstString = (...values) => values.find((v) => typeof v === "string" && v.trim())?.trim() || "";
  const app = document.querySelector("#app");
  const store = app?.__vue_app__?.config?.globalProperties?.$store;
  const roots = [window.__INITIAL_STATE__, window.__NUXT__?.state, store?.state];
  const paths = [
    ["user", "userInfo"], ["user", "info"], ["userInfo"],
    ["account", "userInfo"], ["login", "userInfo"], ["user", "currentUser"], ["currentUser"],
  ];
  for (const root of roots) {
    for (const path of paths) {
      let user = unwrap(root);
      for (const key of path) user = unwrap(user?.[key]);
      if (!user || user.guest || user.isGuest) continue;
      const own = unwrap(user.basicInfo) || user;
      const userId = firstString(user.userId, user.user_id, own.userId, own.user_id);
      const nickname = firstString(own.nickname, own.nickName, own.nick_name);
      if (userId && nickname) {
        return {
          logged_in: true, user_id: userId, nickname,
          avatar: firstString(own.avatar, own.imageb, own.image, user.avatar),
          source: "signed-in-state",
        };
      }
    }
  }

  // Creator's account header belongs to the operator, unlike note-list cards.
  if (creator) {
    const headers = document.querySelectorAll(
      'header .user-info, .header .user-info, .creator-header .user-info, ' +
      '.header .account-info, header .account-info, .header .user, ' +
      '[class*="header"] [class*="userInfo"], [class*="header"] [class*="user-info"]',
    );
    for (const header of headers) {
      const name = header.querySelector(
        '.user-name, .user-nickname, .nickname, .username, .name, [class*="nickName"], [class*="user-name"]',
      );
      const nickname = name?.textContent?.trim() || "";
      if (!nickname || nickname.length > 100 || ["登录", "退出登录", "创作服务平台"].includes(nickname)) continue;
      const avatar = header.querySelector('img[class*="avatar"], [class*="avatar"] img, img');
      return { logged_in: true, nickname, avatar: avatar?.src || "", source: "creator-account-header" };
    }
  }

  // On the consumer site, follow only the authenticated "我" navigation link.
  // Fetching its HTML leaves the current editor/tab and its drafts untouched.
  const ownLink = document.querySelector('.main-container .user a[href*="/user/profile/"], .side-bar .user a[href*="/user/profile/"]');
  const ownPath = ownLink?.getAttribute("href") || "";
  const ownId = ownPath.match(/\/user\/profile\/([a-zA-Z0-9_-]+)/)?.[1];
  if (!creator && ownId) {
    let ownDocument = document;
    if (location.pathname !== `/user/profile/${ownId}`) {
      try {
        const url = new URL(`/user/profile/${ownId}`, location.origin);
        const response = await fetch(url, { credentials: "same-origin", signal: AbortSignal.timeout(6000) });
        if (response.ok) ownDocument = new DOMParser().parseFromString(await response.text(), "text/html");
        else ownDocument = null;
      } catch (_) { ownDocument = null; }
    }
    const nickname = ownDocument?.querySelector(".user-info .user-name, .user-name")?.textContent?.trim();
    if (nickname) {
      const avatar = ownDocument.querySelector('.user-info .avatar, .user-info img, img.user-avatar');
      return { logged_in: true, user_id: ownId, nickname, avatar: avatar?.getAttribute("src") || "", source: "own-navigation-profile" };
    }
  }
  if (!ownId && document.querySelector('.login-container, form[action*="login"], .login-box')) {
    return { logged_in: false };
  }
  return { logged_in: null, error: "尚未读到当前小红书账号，请在此 Chrome 用户资料中打开已登录的小红书页面后刷新" };
})()
