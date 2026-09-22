package main

import (
	"bytes"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
)

func newSunnySessionTestServer(t *testing.T) *Server {
	t.Helper()
	t.Setenv("PYTHON_WORKER_URL", "http://127.0.0.1:1")
	dsn := "file:" + strings.ReplaceAll(t.Name(), "/", "-") + "?mode=memory&cache=shared"
	db, err := gorm.Open(sqlite.Open(dsn), &gorm.Config{})
	if err != nil {
		t.Fatalf("open test database: %v", err)
	}
	sqlDB, err := db.DB()
	if err != nil {
		t.Fatalf("get test database: %v", err)
	}
	sqlDB.SetMaxOpenConns(1)
	if err := db.AutoMigrate(&SunnyMailboxGroup{}, &SunnyMailbox{}, &SunnyAccount{}, &SunnySession{}, &Task{}, &TaskEvent{}, &SunnyKVConfig{}, &SunnyProxy{}); err != nil {
		t.Fatalf("migrate test database: %v", err)
	}
	ensureSunnySchema(db)
	now := time.Now()
	mailbox := SunnyMailbox{
		Email: "session@example.com", Password: "mailbox-password", ClientID: "client-id",
		RefreshToken: "mailbox-refresh-token", Raw: "session@example.com----mailbox-password----client-id----mailbox-refresh-token",
		AccountType: "plus", Status: "已注册", Enabled: true, CreatedAt: now, UpdatedAt: now,
	}
	if err := db.Create(&mailbox).Error; err != nil {
		t.Fatalf("create mailbox: %v", err)
	}
	account := SunnyAccount{
		MailboxID: mailbox.ID, Email: mailbox.Email, Status: "registered", AccountType: "plus",
		AccessToken: "account-access-token", OpenAIRT: "account-refresh-token", CreatedAt: now, UpdatedAt: now,
	}
	if err := db.Create(&account).Error; err != nil {
		t.Fatalf("create account: %v", err)
	}
	if err := db.Create(&SunnySession{
		AccountID: account.ID, Email: mailbox.Email, AccessToken: "session-access-token", RefreshToken: "session-refresh-token",
		SessionJSON: `{"accessToken":"session-access-token"}`, RawMailboxLine: mailbox.Raw, CreatedAt: now, UpdatedAt: now,
	}).Error; err != nil {
		t.Fatalf("create session: %v", err)
	}
	server := &Server{db: db}
	// Unit tests must not depend on the developer machine's default local proxy.
	server.sunnySaveConfig(sunnyCfgProxy, mergeConfig(defaultProxyConfig(), map[string]any{"proxy_enabled": false}))
	return server
}

func TestSunnyAccessTokenProbeUsesPythonWorker(t *testing.T) {
	t.Setenv("PYTHON_WORKER_TOKEN", "worker-secret")
	worker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/probe-access-token" || r.Method != http.MethodPost {
			t.Errorf("unexpected worker request: %s %s", r.Method, r.URL.Path)
			http.Error(w, "unexpected request", http.StatusBadRequest)
			return
		}
		if r.Header.Get("Authorization") != "Bearer worker-secret" {
			t.Errorf("worker authorization header missing")
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Errorf("decode worker request: %v", err)
			http.Error(w, "invalid body", http.StatusBadRequest)
			return
		}
		if payload["access_token"] != "expired-token" || payload["proxy_url"] != "http://proxy.example:8080" {
			t.Errorf("unexpected worker payload: %#v", payload)
			http.Error(w, "invalid payload", http.StatusBadRequest)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"status":  "invalid",
			"error":   "AT 已失效: HTTP 401, code=token_invalidated",
			"traffic": map[string]any{"total_bytes": 321},
		})
	}))
	defer worker.Close()
	t.Setenv("PYTHON_WORKER_URL", worker.URL)

	s := &Server{}
	meter := &sunnyTrafficMeter{}
	status, err := s.sunnyProbeAccessToken("expired-token", "http://proxy.example:8080", meter)
	if status != "invalid" || err == nil || !strings.Contains(err.Error(), "token_invalidated") {
		t.Fatalf("worker probe status=%q err=%v", status, err)
	}
	if meter.totalBytes() != 321 {
		t.Fatalf("worker probe traffic=%d, want 321", meter.totalBytes())
	}
}

func TestSunnyAccessTokenTasksAllowDisjointSessions(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var first SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&first).Error; err != nil {
		t.Fatalf("load first session: %v", err)
	}
	now := time.Now()
	mailbox := SunnyMailbox{Email: "second@example.com", Password: "secret", ClientID: "client", RefreshToken: "refresh", Status: "已注册", Enabled: true, CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&mailbox).Error; err != nil {
		t.Fatalf("create second mailbox: %v", err)
	}
	account := SunnyAccount{Email: mailbox.Email, Status: "已注册", AccessToken: "second-at", CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&account).Error; err != nil {
		t.Fatalf("create second account: %v", err)
	}
	second := SunnySession{AccountID: account.ID, Email: mailbox.Email, AccessToken: account.AccessToken, CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&second).Error; err != nil {
		t.Fatalf("create second session: %v", err)
	}

	if _, err := s.createSunnyAccessTokenCheckTask(map[string]any{"session_ids": []uint{first.ID}}); err != nil {
		t.Fatalf("create first AT task: %v", err)
	}
	if _, err := s.createSunnyAccessTokenCheckTask(map[string]any{"session_ids": []uint{second.ID}}); err != nil {
		t.Fatalf("disjoint AT task was blocked: %v", err)
	}
	if _, err := s.createSunnyAccessTokenCheckTask(map[string]any{"session_ids": []uint{first.ID}}); err == nil || !strings.Contains(err.Error(), "已有 AT 检测任务") {
		t.Fatalf("overlapping AT task was not rejected: %v", err)
	}
}

func TestSunnySessionListDoesNotReturnSecrets(t *testing.T) {
	s := newSunnySessionTestServer(t)
	req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10", nil)
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("list status = %d, body = %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	for _, secret := range []string{"session-access-token", "session-refresh-token", "mailbox-password", "client-id"} {
		if strings.Contains(body, secret) {
			t.Fatalf("session list returned secret %q: %s", secret, body)
		}
	}
	var payload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode list: %v", err)
	}
	if len(payload.Items) != 1 {
		t.Fatalf("list item count = %d", len(payload.Items))
	}
	item := payload.Items[0]
	if item["has_access_token"] != true || item["has_refresh_token"] != true || item["has_secret_key"] != true {
		t.Fatalf("secret presence flags are incorrect: %#v", item)
	}
	if item["plan_type"] != "plus" || item["email"] != "session@example.com" {
		t.Fatalf("summary fields are incorrect: %#v", item)
	}
	if item["phone_bound"] != false {
		t.Fatalf("unbound account was reported as phone bound: %#v", item)
	}
}

func TestSunnySessionListReportsSecretKeyFromRebindMailbox(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatal(err)
	}
	pickup := "https://mail-api.example/api/sunny/domain-mail/pickup?email=rebound%40example.com&token=dmsk_test"
	var account SunnyAccount
	if err := s.db.First(&account, session.AccountID).Error; err != nil {
		t.Fatal(err)
	}
	if err := s.db.Model(&SunnyMailbox{}).Where("id = ?", account.MailboxID).Updates(map[string]any{
		"mailbox_type": "domain", "mailbox_channel": "domain_api", "email": "session@example.com", "rebind_email": "rebound@example.com", "rebind_mailbox_api": pickup,
		"raw": "", "password": "", "client_id": "", "refresh_token": "", "access_key": pickup,
	}).Error; err != nil {
		t.Fatal(err)
	}
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10", nil), nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("list status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var payload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil || len(payload.Items) != 1 {
		t.Fatalf("decode list: err=%v body=%s", err, rec.Body.String())
	}
	if payload.Items[0]["has_secret_key"] != true {
		t.Fatalf("rebind mailbox SK presence flag is false: %#v", payload.Items[0])
	}
}

func TestSunnySessionListReportsCompletedPhoneBinding(t *testing.T) {
	s := newSunnySessionTestServer(t)
	if err := s.db.Model(&SunnyAccount{}).Where("email = ?", "session@example.com").Update("phone_number", "+12025550101").Error; err != nil {
		t.Fatalf("mark account phone binding complete: %v", err)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10", nil)
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, nil)
	var payload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil || len(payload.Items) != 1 {
		t.Fatalf("decode session list: err=%v body=%s", err, rec.Body.String())
	}
	if payload.Items[0]["phone_bound"] != true {
		t.Fatalf("completed phone binding was not reported: %#v", payload.Items[0])
	}
}

func TestSunnySessionListSearchesRebindEmail(t *testing.T) {
	s := newSunnySessionTestServer(t)
	if err := s.db.Model(&SunnyAccount{}).Where("email = ?", "session@example.com").Update("rebind_email", "replacement@example.com").Error; err != nil {
		t.Fatalf("set rebind email: %v", err)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10&q=replacement", nil)
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("search status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var payload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode search: %v", err)
	}
	if len(payload.Items) != 1 || payload.Items[0]["rebind_email"] != "replacement@example.com" {
		t.Fatalf("rebind email search did not return the account: %#v", payload.Items)
	}
}

func TestSunnySessionListFiltersLoginSecretPresence(t *testing.T) {
	s := newSunnySessionTestServer(t)
	mailbox := SunnyMailbox{
		Email: "with-ls@example.com", ChatGPTPassword: "chatgpt-password", TOTPSecret: "totp-secret",
		Status: "已注册", AccountType: "free", Enabled: true, CreatedAt: time.Now(), UpdatedAt: time.Now(),
	}
	if err := s.db.Create(&mailbox).Error; err != nil {
		t.Fatalf("create LS mailbox: %v", err)
	}
	account := SunnyAccount{MailboxID: mailbox.ID, Email: mailbox.Email, Status: "registered", AccountType: "free", AccessToken: "with-ls-at", CreatedAt: time.Now(), UpdatedAt: time.Now()}
	if err := s.db.Create(&account).Error; err != nil {
		t.Fatalf("create LS account: %v", err)
	}
	if err := s.db.Create(&SunnySession{AccountID: account.ID, Email: mailbox.Email, AccessToken: account.AccessToken, CreatedAt: time.Now(), UpdatedAt: time.Now()}).Error; err != nil {
		t.Fatalf("create LS session: %v", err)
	}
	request := func(filter string) []map[string]any {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10&login_secret="+filter, nil)
		s.sunnySessions(rec, req, nil)
		if rec.Code != http.StatusOK {
			t.Fatalf("login secret filter status=%d body=%s", rec.Code, rec.Body.String())
		}
		var payload struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("decode login secret filter: %v", err)
		}
		return payload.Items
	}
	withLS := request("present")
	if len(withLS) != 1 || withLS[0]["email"] != "with-ls@example.com" || withLS[0]["has_login_secret"] != true {
		t.Fatalf("present filter result=%#v", withLS)
	}
	withoutLS := request("missing")
	if len(withoutLS) != 1 || withoutLS[0]["email"] != "session@example.com" || withoutLS[0]["has_login_secret"] != false {
		t.Fatalf("missing filter result=%#v", withoutLS)
	}
}

func TestSunnySessionListFiltersRebindEmailPresence(t *testing.T) {
	s := newSunnySessionTestServer(t)
	if err := s.db.Model(&SunnyAccount{}).Where("email = ?", "session@example.com").Update("rebind_email", "rebound@example.com").Error; err != nil {
		t.Fatalf("set rebind email: %v", err)
	}
	now := time.Now()
	mailbox := SunnyMailbox{Email: "not-rebound@example.com", Status: "已注册", AccountType: "free", Enabled: true, CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&mailbox).Error; err != nil {
		t.Fatalf("create mailbox: %v", err)
	}
	account := SunnyAccount{MailboxID: mailbox.ID, Email: mailbox.Email, Status: "registered", AccountType: "free", CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&account).Error; err != nil {
		t.Fatalf("create account: %v", err)
	}
	if err := s.db.Create(&SunnySession{AccountID: account.ID, Email: mailbox.Email, CreatedAt: now, UpdatedAt: now}).Error; err != nil {
		t.Fatalf("create session: %v", err)
	}

	request := func(filter string) []map[string]any {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10&rebind_email="+filter, nil)
		s.sunnySessions(rec, req, nil)
		if rec.Code != http.StatusOK {
			t.Fatalf("rebind email filter status=%d body=%s", rec.Code, rec.Body.String())
		}
		var payload struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("decode rebind email filter: %v", err)
		}
		return payload.Items
	}
	withRebind := request("present")
	if len(withRebind) != 1 || withRebind[0]["email"] != "session@example.com" {
		t.Fatalf("present rebind filter result=%#v", withRebind)
	}
	withoutRebind := request("missing")
	if len(withoutRebind) != 1 || withoutRebind[0]["email"] != mailbox.Email {
		t.Fatalf("missing rebind filter result=%#v", withoutRebind)
	}
}

func TestSunnyMailboxListFiltersCredentialPresence(t *testing.T) {
	s := newSunnySessionTestServer(t)
	now := time.Now()
	createMailbox := func(email string, password, totp, rebind string) uint {
		t.Helper()
		mailbox := SunnyMailbox{Email: email, Password: "mail-password", ClientID: "client", RefreshToken: "refresh", ChatGPTPassword: password, TOTPSecret: totp, RebindEmail: rebind, Status: "已注册", AccountType: "free", Enabled: true, CreatedAt: now, UpdatedAt: now}
		if err := s.db.Create(&mailbox).Error; err != nil {
			t.Fatalf("create mailbox %s: %v", email, err)
		}
		return mailbox.ID
	}
	createMailbox("password-only@example.com", "chat-password", "", "")
	createMailbox("password-2fa-rebound@example.com", "chat-password", "totp-secret", "rebound@example.com")
	createMailbox("empty@example.com", "", "", "")

	request := func(query string) []map[string]any {
		rec := httptest.NewRecorder()
		s.sunnyMailboxes(rec, httptest.NewRequest(http.MethodGet, "/api/sunny/mailboxes?summary=true&page=1&page_size=20&"+query, nil), nil)
		if rec.Code != http.StatusOK {
			t.Fatalf("mailbox filter status=%d body=%s", rec.Code, rec.Body.String())
		}
		var payload struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("decode mailbox filter: %v", err)
		}
		return payload.Items
	}
	withPassword := request("password=present")
	if len(withPassword) != 2 {
		t.Fatalf("password present result=%#v", withPassword)
	}
	withTwoFactor := request("totp=present")
	if len(withTwoFactor) != 1 || withTwoFactor[0]["email"] != "password-2fa-rebound@example.com" {
		t.Fatalf("2FA present result=%#v", withTwoFactor)
	}
	withRebind := request("rebind_email=present")
	if len(withRebind) != 1 || withRebind[0]["email"] != "password-2fa-rebound@example.com" {
		t.Fatalf("rebind present result=%#v", withRebind)
	}
	withoutPassword := request("password=missing")
	if len(withoutPassword) != 2 {
		t.Fatalf("password missing result=%#v", withoutPassword)
	}
}

func TestSunnySessionListFiltersEligibleTrialCountriesWithAND(t *testing.T) {
	s := newSunnySessionTestServer(t)
	now := time.Now()
	createSession := func(email string, results map[string]string) uint {
		t.Helper()
		raw := dumpJSON(results)
		mailbox := SunnyMailbox{Email: email, Status: "已注册", AccountType: "free", TrialCountryResultsJSON: raw, Enabled: true, CreatedAt: now, UpdatedAt: now}
		if err := s.db.Create(&mailbox).Error; err != nil {
			t.Fatalf("create mailbox %s: %v", email, err)
		}
		account := SunnyAccount{MailboxID: mailbox.ID, Email: email, Status: "registered", AccountType: "free", TrialCountryResultsJSON: raw, CreatedAt: now, UpdatedAt: now}
		if err := s.db.Create(&account).Error; err != nil {
			t.Fatalf("create account %s: %v", email, err)
		}
		session := SunnySession{AccountID: account.ID, Email: email, CreatedAt: now, UpdatedAt: now}
		if err := s.db.Create(&session).Error; err != nil {
			t.Fatalf("create session %s: %v", email, err)
		}
		return session.ID
	}
	bothID := createSession("jp-br@example.com", map[string]string{"JP": "eligible", "BR": "eligible"})
	createSession("jp-only@example.com", map[string]string{"JP": "eligible", "BR": "ineligible"})
	createSession("jp-vn@example.com", map[string]string{"JP": "eligible", "VN": "ineligible"})

	request := func(query string) (items []map[string]any, ids []uint, options []string) {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?"+query, nil)
		s.sunnySessions(rec, req, nil)
		if rec.Code != http.StatusOK {
			t.Fatalf("trial country filter status=%d body=%s", rec.Code, rec.Body.String())
		}
		var payload struct {
			Items               []map[string]any `json:"items"`
			IDs                 []uint           `json:"ids"`
			TrialCountryOptions []string         `json:"trial_country_options"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("decode trial country filter: %v", err)
		}
		return payload.Items, payload.IDs, payload.TrialCountryOptions
	}
	jpItems, _, options := request("page=1&page_size=10&trial_countries=JP")
	if len(jpItems) != 3 {
		t.Fatalf("JP filter returned %d items: %#v", len(jpItems), jpItems)
	}
	if strings.Join(options, ",") != "BR,JP,VN" {
		t.Fatalf("trial country options=%v", options)
	}
	andItems, _, _ := request("page=1&page_size=10&trial_countries=JP,BR")
	if len(andItems) != 1 || andItems[0]["email"] != "jp-br@example.com" {
		t.Fatalf("JP+BR AND filter result=%#v", andItems)
	}
	_, selectedIDs, _ := request("selection=all&trial_countries=BR,JP")
	if len(selectedIDs) != 1 || selectedIDs[0] != bothID {
		t.Fatalf("JP+BR selection ids=%v want [%d]", selectedIDs, bothID)
	}
}

func TestSunnyMailboxListSortsRebindEmailBeforePagination(t *testing.T) {
	s := newSunnySessionTestServer(t)
	if err := s.db.Model(&SunnyMailbox{}).Where("email = ?", "session@example.com").Update("rebind_email", "m-replacement@example.com").Error; err != nil {
		t.Fatal(err)
	}
	for _, email := range []string{"z-rebind@example.com", "empty@example.com", "a-rebind@example.com"} {
		mailbox := SunnyMailbox{Email: email, RebindEmail: strings.TrimSuffix(strings.TrimSuffix(email, "-rebind@example.com"), "@example.com") + "-replacement@example.com", Status: "未注册", Enabled: true, CreatedAt: time.Now(), UpdatedAt: time.Now()}
		if email == "empty@example.com" {
			mailbox.RebindEmail = ""
		}
		if err := s.db.Create(&mailbox).Error; err != nil {
			t.Fatalf("create mailbox %s: %v", email, err)
		}
	}
	request := func(order string) []map[string]any {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "/api/sunny/mailboxes?page=1&page_size=2&sort_by=rebind_email&sort_order="+order, nil)
		s.sunnyMailboxes(rec, req, nil)
		if rec.Code != http.StatusOK {
			t.Fatalf("mailbox list status = %d, body = %s", rec.Code, rec.Body.String())
		}
		var payload struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("decode mailbox list: %v", err)
		}
		return payload.Items
	}
	desc := request("desc")
	if len(desc) != 2 || desc[0]["rebind_email"] != "z-replacement@example.com" || desc[1]["rebind_email"] != "m-replacement@example.com" {
		t.Fatalf("desc rebind order = %#v", desc)
	}
	asc := request("asc")
	if len(asc) != 2 || asc[0]["rebind_email"] != "" || asc[1]["rebind_email"] != "a-replacement@example.com" {
		t.Fatalf("asc rebind order = %#v", asc)
	}
}

func TestSunnySessionListSortsRebindEmailBeforePagination(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var base SunnyMailbox
	if err := s.db.Where("email = ?", "session@example.com").First(&base).Error; err != nil {
		t.Fatal(err)
	}
	if err := s.db.Model(&base).Update("rebind_email", "z-replacement@example.com").Error; err != nil {
		t.Fatal(err)
	}
	for _, email := range []string{"empty-session@example.com", "b-session@example.com"} {
		mailbox := SunnyMailbox{Email: email, Status: "已注册", Enabled: true, CreatedAt: time.Now(), UpdatedAt: time.Now()}
		if err := s.db.Create(&mailbox).Error; err != nil {
			t.Fatal(err)
		}
		account := SunnyAccount{MailboxID: mailbox.ID, Email: email, Status: "registered", AccountType: "plus", AccessToken: "at-" + email, CreatedAt: time.Now(), UpdatedAt: time.Now()}
		if err := s.db.Create(&account).Error; err != nil {
			t.Fatal(err)
		}
		if err := s.db.Create(&SunnySession{AccountID: account.ID, Email: email, AccessToken: account.AccessToken, CreatedAt: time.Now(), UpdatedAt: time.Now()}).Error; err != nil {
			t.Fatal(err)
		}
	}
	if err := s.db.Model(&SunnyMailbox{}).Where("email = ?", "b-session@example.com").Update("rebind_email", "b-replacement@example.com").Error; err != nil {
		t.Fatal(err)
	}
	request := func(order string, page int) []map[string]any {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, fmt.Sprintf("/api/sunny/sessions?page=%d&page_size=2&sort_by=rebind_email&sort_order=%s", page, order), nil)
		s.sunnySessions(rec, req, nil)
		if rec.Code != http.StatusOK {
			t.Fatalf("session list status = %d, body = %s", rec.Code, rec.Body.String())
		}
		var payload struct {
			Items []map[string]any `json:"items"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("decode session list: %v", err)
		}
		return payload.Items
	}
	desc := request("desc", 1)
	if len(desc) != 2 || desc[0]["rebind_email"] != "z-replacement@example.com" || desc[1]["rebind_email"] != "b-replacement@example.com" {
		t.Fatalf("desc session rebind order = %#v", desc)
	}
	asc := request("asc", 1)
	if len(asc) != 2 || asc[0]["rebind_email"] != "" || asc[1]["rebind_email"] != "b-replacement@example.com" {
		t.Fatalf("asc session rebind order = %#v", asc)
	}
}

func TestSunnyMailboxAndSessionListsShareMailboxIdentityFields(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var mailbox SunnyMailbox
	if err := s.db.Where("email = ?", "session@example.com").First(&mailbox).Error; err != nil {
		t.Fatal(err)
	}
	if err := s.db.Model(&mailbox).Updates(map[string]any{
		"rebind_email": "replacement@example.com", "rebind_mailbox_api": "https://mail.example/pickup?email=replacement%40example.com&token=dmsk_test",
		"status": "已接码", "account_type": "team",
	}).Error; err != nil {
		t.Fatal(err)
	}
	mailboxRec := httptest.NewRecorder()
	s.sunnyMailboxes(mailboxRec, httptest.NewRequest(http.MethodGet, "/api/sunny/mailboxes?page=1&page_size=10&q=session@example.com", nil), nil)
	var mailboxPayload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(mailboxRec.Body.Bytes(), &mailboxPayload); err != nil || len(mailboxPayload.Items) != 1 {
		t.Fatalf("decode mailbox list: err=%v body=%s", err, mailboxRec.Body.String())
	}
	sessionRec := httptest.NewRecorder()
	s.sunnySessions(sessionRec, httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10&q=session@example.com", nil), nil)
	var sessionPayload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(sessionRec.Body.Bytes(), &sessionPayload); err != nil || len(sessionPayload.Items) != 1 {
		t.Fatalf("decode session list: err=%v body=%s", err, sessionRec.Body.String())
	}
	mailboxItem, sessionItem := mailboxPayload.Items[0], sessionPayload.Items[0]
	for _, field := range []string{"email", "rebind_email", "group_id", "status", "plan_type"} {
		if text(mailboxItem[field]) != text(sessionItem[field]) {
			t.Fatalf("shared field %s differs: mailbox=%#v session=%#v", field, mailboxItem[field], sessionItem[field])
		}
	}
}

func TestSunnyMailboxAndSessionATFieldsUseSameMailboxSource(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var mailbox SunnyMailbox
	if err := s.db.Where("email = ?", "session@example.com").First(&mailbox).Error; err != nil {
		t.Fatal(err)
	}
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatal(err)
	}
	encode := func(exp int64) string {
		return "eyJhbGciOiJSUzI1NiJ9." + base64.RawURLEncoding.EncodeToString([]byte(fmt.Sprintf(`{"exp":%d}`, exp))) + ".signature"
	}
	latest := encode(1900000000)
	if err := s.db.Model(&SunnySession{}).Where("id = ?", session.ID).Updates(map[string]any{
		"access_token": encode(1700000000), "session_json": fmt.Sprintf(`{"accessToken":%q}`, latest),
	}).Error; err != nil {
		t.Fatal(err)
	}
	mailboxRec := httptest.NewRecorder()
	mailboxPath := fmt.Sprintf("/api/sunny/mailboxes/%d/field?name=access_token", mailbox.ID)
	s.handleSunny(mailboxRec, httptest.NewRequest(http.MethodGet, mailboxPath, nil), fmt.Sprintf("mailboxes/%d/field", mailbox.ID))
	var mailboxPayload map[string]any
	if err := json.Unmarshal(mailboxRec.Body.Bytes(), &mailboxPayload); err != nil {
		t.Fatalf("decode mailbox AT: %v", err)
	}
	sessionRec := httptest.NewRecorder()
	sessionPath := fmt.Sprintf("/api/sunny/sessions/%d/field?name=access_token", session.ID)
	s.handleSunny(sessionRec, httptest.NewRequest(http.MethodGet, sessionPath, nil), fmt.Sprintf("sessions/%d/field", session.ID))
	var sessionPayload map[string]any
	if err := json.Unmarshal(sessionRec.Body.Bytes(), &sessionPayload); err != nil {
		t.Fatalf("decode session AT: %v", err)
	}
	if mailboxPayload["value"] != latest || sessionPayload["value"] != latest {
		t.Fatalf("AT values differ: mailbox=%v session=%v latest=%v", mailboxPayload["value"], sessionPayload["value"], latest)
	}
}

func TestSunnySessionListDeduplicatesCaseInsensitiveLegacyRows(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var account SunnyAccount
	if err := s.db.Where("email = ?", "session@example.com").First(&account).Error; err != nil {
		t.Fatalf("load account: %v", err)
	}
	now := time.Now()
	legacy := SunnySession{
		AccountID:   account.ID,
		Email:       "SESSION@EXAMPLE.COM",
		AccessToken: "newer-token",
		CreatedAt:   now,
		UpdatedAt:   now.Add(time.Minute),
	}
	if err := s.db.Create(&legacy).Error; err != nil {
		t.Fatalf("create legacy duplicate session: %v", err)
	}
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10", nil), nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("list status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var payload struct {
		Items []map[string]any `json:"items"`
		Total int              `json:"total"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode list: %v", err)
	}
	if payload.Total != 1 || len(payload.Items) != 1 {
		t.Fatalf("deduplicated list = total %d items %d, want one row: %s", payload.Total, len(payload.Items), rec.Body.String())
	}
	if uint(intValue(payload.Items[0]["id"], 0)) != legacy.ID {
		t.Fatalf("deduplicated row did not keep newest session: %#v", payload.Items[0])
	}
}

func TestSunnySessionUpdateSynchronizesMailboxAndAccountMetadata(t *testing.T) {
	s := newSunnySessionTestServer(t)
	group := SunnyMailboxGroup{Name: "Target Group"}
	if err := s.db.Create(&group).Error; err != nil {
		t.Fatalf("create mailbox group: %v", err)
	}
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	body, err := json.Marshal(map[string]any{
		"status":        "\u767b\u5f55\u5237\u65b0",
		"group_id":      group.ID,
		"plan_type":     "team",
		"access_token":  "updated-access-token",
		"refresh_token": "updated-refresh-token",
	})
	if err != nil {
		t.Fatalf("encode update request: %v", err)
	}
	req := httptest.NewRequest(http.MethodPut, "/api/sunny/sessions/"+strconv.Itoa(int(session.ID)), bytes.NewReader(body))
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, []string{strconv.Itoa(int(session.ID))})
	if rec.Code != http.StatusOK {
		t.Fatalf("update status = %d, body = %s", rec.Code, rec.Body.String())
	}

	var mailbox SunnyMailbox
	if err := s.db.Where("email = ?", session.Email).First(&mailbox).Error; err != nil {
		t.Fatalf("load updated mailbox: %v", err)
	}
	if mailbox.GroupID != group.ID || mailbox.AccountType != "team" || mailbox.Status != "\u767b\u5f55\u5237\u65b0" || mailbox.OpenAIRT != "updated-refresh-token" {
		t.Fatalf("mailbox metadata was not synchronized: %#v", mailbox)
	}
	var account SunnyAccount
	if err := s.db.Where("email = ?", session.Email).First(&account).Error; err != nil {
		t.Fatalf("load updated account: %v", err)
	}
	if account.GroupName != group.Name || account.AccountType != "team" || account.Status != "\u767b\u5f55\u5237\u65b0" || account.AccessToken != "updated-access-token" || account.OpenAIRT != "updated-refresh-token" {
		t.Fatalf("account metadata was not synchronized: %#v", account)
	}
	var updatedSession SunnySession
	if err := s.db.First(&updatedSession, session.ID).Error; err != nil {
		t.Fatalf("load updated session: %v", err)
	}
	if updatedSession.AccessToken != "updated-access-token" || updatedSession.RefreshToken != "updated-refresh-token" {
		t.Fatalf("session tokens were not updated: %#v", updatedSession)
	}
	var payload map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode update response: %v", err)
	}
	if payload["group_name"] != group.Name || payload["plan_type"] != "team" || payload["status"] != "\u767b\u5f55\u5237\u65b0" {
		t.Fatalf("updated session response is incomplete: %#v", payload)
	}
}

func TestSunnySessionRenameSynchronizesMailboxAccountAndCredentialLine(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	body, err := json.Marshal(map[string]any{"email": "renamed@example.com"})
	if err != nil {
		t.Fatalf("encode rename request: %v", err)
	}
	req := httptest.NewRequest(http.MethodPut, "/api/sunny/sessions/"+strconv.Itoa(int(session.ID)), bytes.NewReader(body))
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, []string{strconv.Itoa(int(session.ID))})
	if rec.Code != http.StatusOK {
		t.Fatalf("rename status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var mailbox SunnyMailbox
	if err := s.db.Where("email = ?", "renamed@example.com").First(&mailbox).Error; err != nil {
		t.Fatalf("load renamed mailbox: %v", err)
	}
	if mailbox.Raw != "renamed@example.com----mailbox-password----client-id----mailbox-refresh-token" {
		t.Fatalf("mailbox credential line was not renamed: %q", mailbox.Raw)
	}
	var account SunnyAccount
	if err := s.db.Where("email = ?", "renamed@example.com").First(&account).Error; err != nil {
		t.Fatalf("load renamed account: %v", err)
	}
	var updated SunnySession
	if err := s.db.First(&updated, session.ID).Error; err != nil {
		t.Fatalf("load renamed session: %v", err)
	}
	if updated.Email != "renamed@example.com" || updated.RawMailboxLine != mailbox.Raw {
		t.Fatalf("session rename was not synchronized: %#v", updated)
	}
	var oldCount int64
	s.db.Model(&SunnyMailbox{}).Where("email = ?", "session@example.com").Count(&oldCount)
	if oldCount != 0 {
		t.Fatalf("old mailbox email still exists: %d", oldCount)
	}
}

func TestSunnyMailboxRenameSynchronizesLinkedRecords(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var mailbox SunnyMailbox
	if err := s.db.Where("email = ?", "session@example.com").First(&mailbox).Error; err != nil {
		t.Fatalf("load mailbox: %v", err)
	}
	body, err := json.Marshal(map[string]any{"email": "mailbox-renamed@example.com"})
	if err != nil {
		t.Fatalf("encode rename request: %v", err)
	}
	req := httptest.NewRequest(http.MethodPut, "/api/sunny/mailboxes/"+strconv.Itoa(int(mailbox.ID)), bytes.NewReader(body))
	rec := httptest.NewRecorder()
	s.sunnyMailboxes(rec, req, []string{strconv.Itoa(int(mailbox.ID))})
	if rec.Code != http.StatusOK {
		t.Fatalf("rename status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var account SunnyAccount
	if err := s.db.Where("email = ?", "mailbox-renamed@example.com").First(&account).Error; err != nil {
		t.Fatalf("load renamed account: %v", err)
	}
	var session SunnySession
	if err := s.db.Where("email = ?", "mailbox-renamed@example.com").First(&session).Error; err != nil {
		t.Fatalf("load renamed session: %v", err)
	}
	if session.RawMailboxLine != "mailbox-renamed@example.com----mailbox-password----client-id----mailbox-refresh-token" {
		t.Fatalf("linked session credential line was not renamed: %q", session.RawMailboxLine)
	}
}

func TestSunnyMailboxRebindFieldsAreIndependentAndAPIAcceptsCustomCredentials(t *testing.T) {
	tests := []struct {
		name               string
		body               map[string]any
		wantRebindEmail    string
		wantRebindAPI      string
		wantMailboxType    string
		wantMailboxChannel string
		wantAccessKey      string
	}{
		{
			name:            "email only",
			body:            map[string]any{"rebind_email": "rebound@example.com"},
			wantRebindEmail: "rebound@example.com", wantMailboxType: "microsoft", wantMailboxChannel: "outlook",
		},
		{
			name:          "api only",
			body:          map[string]any{"rebind_mailbox_api": "custom-provider::mailbox-token"},
			wantRebindAPI: "custom-provider::mailbox-token", wantMailboxType: "microsoft", wantMailboxChannel: "outlook",
		},
		{
			name:            "custom complete credential",
			body:            map[string]any{"rebind_email": "rebound@example.com", "rebind_mailbox_api": "custom-provider::mailbox-token"},
			wantRebindEmail: "rebound@example.com", wantRebindAPI: "custom-provider::mailbox-token",
			wantMailboxType: "domain", wantMailboxChannel: "domain_api", wantAccessKey: "custom-provider::mailbox-token",
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			s := newSunnySessionTestServer(t)
			var mailbox SunnyMailbox
			if err := s.db.Where("email = ?", "session@example.com").First(&mailbox).Error; err != nil {
				t.Fatal(err)
			}
			body, _ := json.Marshal(test.body)
			req := httptest.NewRequest(http.MethodPut, "/api/sunny/mailboxes/"+strconv.Itoa(int(mailbox.ID)), bytes.NewReader(body))
			rec := httptest.NewRecorder()
			s.sunnyMailboxes(rec, req, []string{strconv.Itoa(int(mailbox.ID))})
			if rec.Code != http.StatusOK {
				t.Fatalf("update status=%d body=%s", rec.Code, rec.Body.String())
			}
			if err := s.db.First(&mailbox, mailbox.ID).Error; err != nil {
				t.Fatal(err)
			}
			if mailbox.RebindEmail != test.wantRebindEmail || mailbox.RebindMailboxAPI != test.wantRebindAPI ||
				mailbox.MailboxType != test.wantMailboxType || mailbox.MailboxChannel != test.wantMailboxChannel || mailbox.AccessKey != test.wantAccessKey {
				t.Fatalf("unexpected mailbox rebind state: %#v", mailbox)
			}
			var account SunnyAccount
			if err := s.db.Where("mailbox_id = ?", mailbox.ID).First(&account).Error; err != nil {
				t.Fatal(err)
			}
			if account.RebindEmail != test.wantRebindEmail || account.RebindMailboxAPI != test.wantRebindAPI {
				t.Fatalf("account rebind fields were not synchronized: %#v", account)
			}
		})
	}
}

func TestSunnySessionRebindAPIAllowsIndependentCustomValue(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatal(err)
	}
	body, _ := json.Marshal(map[string]any{"rebind_mailbox_api": "opaque custom mailbox credential"})
	req := httptest.NewRequest(http.MethodPut, "/api/sunny/sessions/"+strconv.Itoa(int(session.ID)), bytes.NewReader(body))
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, []string{strconv.Itoa(int(session.ID))})
	if rec.Code != http.StatusOK {
		t.Fatalf("update status=%d body=%s", rec.Code, rec.Body.String())
	}
	var account SunnyAccount
	if err := s.db.First(&account, session.AccountID).Error; err != nil {
		t.Fatal(err)
	}
	var mailbox SunnyMailbox
	if err := s.db.First(&mailbox, account.MailboxID).Error; err != nil {
		t.Fatal(err)
	}
	if account.RebindEmail != "" || mailbox.RebindEmail != "" || account.RebindMailboxAPI != "opaque custom mailbox credential" || mailbox.RebindMailboxAPI != account.RebindMailboxAPI {
		t.Fatalf("independent custom API was not saved: account=%#v mailbox=%#v", account, mailbox)
	}
	if mailbox.MailboxType != "microsoft" || mailbox.MailboxChannel != "outlook" {
		t.Fatalf("partial rebind metadata changed the original mailbox provider: %#v", mailbox)
	}
}

func TestSunnySessionRenameRejectsExistingEmail(t *testing.T) {
	s := newSunnySessionTestServer(t)
	if err := s.db.Create(&SunnyMailbox{Email: "other@example.com", Status: "未注册", Enabled: true}).Error; err != nil {
		t.Fatalf("create conflicting mailbox: %v", err)
	}
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	body, _ := json.Marshal(map[string]any{"email": "other@example.com"})
	req := httptest.NewRequest(http.MethodPut, "/api/sunny/sessions/"+strconv.Itoa(int(session.ID)), bytes.NewReader(body))
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, []string{strconv.Itoa(int(session.ID))})
	if rec.Code != http.StatusConflict {
		t.Fatalf("conflict status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var unchanged SunnySession
	if err := s.db.First(&unchanged, session.ID).Error; err != nil {
		t.Fatalf("load unchanged session: %v", err)
	}
	if unchanged.Email != "session@example.com" {
		t.Fatalf("conflicting rename changed session email: %q", unchanged.Email)
	}
}

func TestSunnySessionListUsesJWTExpiryInShanghai(t *testing.T) {
	s := newSunnySessionTestServer(t)
	previousLocation := sunnyApplicationLocation
	sunnyApplicationLocation = time.FixedZone("Asia/Shanghai", 8*60*60)
	t.Cleanup(func() { sunnyApplicationLocation = previousLocation })

	exp := int64(1893456000)
	payload := base64.RawURLEncoding.EncodeToString([]byte(`{"exp":1893456000}`))
	accessToken := "header." + payload + ".signature"
	storedWrong := time.Unix(exp, 0).Add(8 * time.Hour)
	if err := s.db.Model(&SunnySession{}).Where("email = ?", "session@example.com").Updates(map[string]any{
		"access_token": accessToken,
		"expires_at":   sql.NullTime{Time: storedWrong, Valid: true},
	}).Error; err != nil {
		t.Fatalf("update session expiry: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10", nil)
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("list status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var response struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode session list: %v", err)
	}
	want := time.Unix(exp, 0).In(sunnyApplicationLocation).Format(time.RFC3339)
	if got := response.Items[0]["access_token_expires_at"]; got != want {
		t.Fatalf("access token expiry = %v, want %s", got, want)
	}
}

func TestEnsureShanghaiTimestampStorageNormalizesLegacyValues(t *testing.T) {
	s := newSunnySessionTestServer(t)
	if err := s.db.Exec("UPDATE sunny_sessions SET expires_at = ? WHERE email = ?", "2026-07-29 12:34:56", "session@example.com").Error; err != nil {
		t.Fatalf("write legacy timestamp: %v", err)
	}

	ensureShanghaiTimestampStorage(s.db)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load normalized session: %v", err)
	}
	_, offset := session.ExpiresAt.Time.Zone()
	if !session.ExpiresAt.Valid || offset != 8*60*60 || session.ExpiresAt.Time.Hour() != 12 {
		t.Fatalf("normalized expiry = %v, valid=%v, offset=%d", session.ExpiresAt.Time, session.ExpiresAt.Valid, offset)
	}
}

func TestSunnySessionFieldIsLoadedOnDemand(t *testing.T) {
	s := newSunnySessionTestServer(t)
	for _, test := range []struct {
		field string
		want  string
	}{
		{field: "access_token", want: "session-access-token"},
		{field: "refresh_token", want: "session-refresh-token"},
		{field: "secret_key", want: "session@example.com----mailbox-password----client-id----mailbox-refresh-token"},
	} {
		req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions/1/field?name="+test.field, nil)
		rec := httptest.NewRecorder()
		s.sunnySessions(rec, req, []string{"1", "field"})
		if rec.Code != http.StatusOK {
			t.Fatalf("field %s status = %d, body = %s", test.field, rec.Code, rec.Body.String())
		}
		var payload map[string]string
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatalf("decode field %s: %v", test.field, err)
		}
		if payload["value"] != test.want {
			t.Fatalf("field %s = %q, want %q", test.field, payload["value"], test.want)
		}
		if rec.Header().Get("Cache-Control") != "no-store" {
			t.Fatalf("field %s response is cacheable", test.field)
		}
	}
}

func TestSunnySessionRebindEditSynchronizesMailboxCredentials(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatal(err)
	}
	var original SunnyMailbox
	if err := s.db.Where("email = ?", session.Email).First(&original).Error; err != nil {
		t.Fatal(err)
	}
	if err := s.db.Model(&original).Updates(map[string]any{"chat_gpt_password": "chat-password", "totp_secret": "JBSWY3DPEHPK3PXP"}).Error; err != nil {
		t.Fatal(err)
	}
	pickup := "https://mail-api.example/api/sunny/domain-mail/pickup?email=rebound%40example.com&token=dmsk_test"
	body, _ := json.Marshal(map[string]any{"email": session.Email, "rebind_email": "rebound@example.com", "rebind_mailbox_api": pickup})
	req := httptest.NewRequest(http.MethodPut, "/api/sunny/sessions/"+strconv.Itoa(int(session.ID)), bytes.NewReader(body))
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, []string{strconv.Itoa(int(session.ID))})
	if rec.Code != http.StatusOK {
		t.Fatalf("update status=%d body=%s", rec.Code, rec.Body.String())
	}

	var mailbox SunnyMailbox
	if err := s.db.First(&mailbox, original.ID).Error; err != nil {
		t.Fatal(err)
	}
	if mailbox.Email != original.Email || mailbox.RebindEmail != "rebound@example.com" || mailbox.RebindMailboxAPI != pickup || mailbox.MailboxType != "domain" || mailbox.MailboxChannel != "domain_api" {
		t.Fatalf("mailbox rebind metadata not synchronized: %#v", mailbox)
	}
	var account SunnyAccount
	if err := s.db.First(&account, session.AccountID).Error; err != nil {
		t.Fatal(err)
	}
	if account.RebindEmail != mailbox.RebindEmail || account.RebindMailboxAPI != pickup || account.MailboxID != mailbox.ID {
		t.Fatalf("account rebind metadata not synchronized: %#v", account)
	}
	for field, want := range map[string]string{
		"secret_key":   pickupWithEmail("rebound@example.com", pickup),
		"login_secret": "rebound@example.com----chat-password----JBSWY3DPEHPK3PXP",
	} {
		value, err := s.sunnySessionFieldValue(session.ID, field)
		if err != nil || value != want {
			t.Fatalf("%s=%q want=%q err=%v", field, value, want, err)
		}
	}
}

func pickupWithEmail(email, pickup string) string { return email + "----" + pickup }

func TestReconcileSunnyRebindCredentialsBackfillsMailboxAndSession(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatal(err)
	}
	var account SunnyAccount
	if err := s.db.First(&account, session.AccountID).Error; err != nil {
		t.Fatal(err)
	}
	pickup := "https://mail-api.example/api/sunny/domain-mail/pickup?email=existing-rebind%40example.com&token=dmsk_existing"
	if err := s.db.Model(&account).Updates(map[string]any{"rebind_email": "existing-rebind@example.com", "rebind_mailbox_api": pickup}).Error; err != nil {
		t.Fatal(err)
	}
	if err := s.db.Model(&account).Update("email", "existing-rebind@example.com").Error; err != nil {
		t.Fatal(err)
	}
	if err := s.db.Model(&session).Update("email", "existing-rebind@example.com").Error; err != nil {
		t.Fatal(err)
	}
	reconcileSunnyRebindCredentials(s.db)
	var mailbox SunnyMailbox
	if err := s.db.First(&mailbox, account.MailboxID).Error; err != nil {
		t.Fatal(err)
	}
	if mailbox.Email != "session@example.com" || mailbox.RebindEmail != "existing-rebind@example.com" || mailbox.RebindMailboxAPI != pickup || mailbox.AccessKey != pickup {
		t.Fatalf("mailbox was not reconciled: %#v", mailbox)
	}
	if err := s.db.First(&session, session.ID).Error; err != nil {
		t.Fatal(err)
	}
	if err := s.db.First(&account, account.ID).Error; err != nil {
		t.Fatal(err)
	}
	if account.Email != "session@example.com" || session.Email != "session@example.com" {
		t.Fatalf("rebind identity was not restored: account=%q session=%q", account.Email, session.Email)
	}
	if session.RawMailboxLine != pickupWithEmail(mailbox.RebindEmail, pickup) {
		t.Fatalf("session credential was not reconciled: %q", session.RawMailboxLine)
	}
}

func TestSunnyHealthBanMarkers(t *testing.T) {
	for _, title := range []string{
		"Access Deactivated",
		"Your account [C-75ROCz5moZsB] has been deactivated",
		"账户已被停用",
		"アカウントが無効になりました",
	} {
		if !sunnyHealthBanMarker.MatchString(title) {
			t.Fatalf("title %q was not recognized as banned", title)
		}
	}
	for _, title := range []string{
		"Welcome to ChatGPT",
		"Access restored",
		"Account notice [C-75ROCz5moZsB]",
		"OpenAI ChatGPT - 規定違反と無効化についての警告 [C-gzvRqiPNpDSe]",
		"規定違反が繰り返される場合は、サービスへのアクセスを無効にする可能性があります。",
	} {
		if sunnyHealthBanMarker.MatchString(title) {
			t.Fatalf("title %q was incorrectly recognized as banned", title)
		}
	}
}

func TestSunnyScheduledHealthCandidatesIncludeRegisteredMailboxesAcrossGroups(t *testing.T) {
	s := newSunnySessionTestServer(t)
	groupA := SunnyMailboxGroup{Name: "分组 A"}
	groupB := SunnyMailboxGroup{Name: "分组 B"}
	s.db.Create(&groupA)
	s.db.Create(&groupB)

	rows := []SunnyMailbox{
		{GroupID: groupA.ID, Email: "group-a@example.com", ClientID: "client-a", RefreshToken: "refresh-a", Status: "已注册", Enabled: true},
		{GroupID: groupB.ID, Email: "group-b@example.com", ClientID: "client-b", RefreshToken: "refresh-b", Status: "已接码", Enabled: true},
		{GroupID: groupB.ID, Email: "unused@example.com", ClientID: "client-u", RefreshToken: "refresh-u", Status: "未注册", Enabled: true},
		{GroupID: groupB.ID, Email: "banned@example.com", ClientID: "client-x", RefreshToken: "refresh-x", Status: "已封禁", Enabled: true},
	}
	for index := range rows {
		if err := s.db.Create(&rows[index]).Error; err != nil {
			t.Fatalf("create mailbox: %v", err)
		}
	}

	candidates, skipped, err := s.sunnyHealthCandidates(nil, true)
	if err != nil {
		t.Fatalf("scheduled candidates: %v", err)
	}
	found := map[string]bool{}
	for _, candidate := range candidates {
		found[candidate.Email] = true
	}
	if !found["group-a@example.com"] || !found["group-b@example.com"] {
		t.Fatalf("registered mailboxes in non-default groups were omitted: %#v", candidates)
	}
	if found["unused@example.com"] || found["banned@example.com"] {
		t.Fatalf("ineligible mailbox was scheduled: %#v", candidates)
	}
	if skipped < 1 {
		t.Fatalf("banned account should be reported as skipped")
	}
}

func TestSunnyAccessTokenProbeClassifiesAuthenticationResponses(t *testing.T) {
	originalEndpoint := sunnyProbeAccessTokenEndpoint
	defer func() { sunnyProbeAccessTokenEndpoint = originalEndpoint }()

	tests := []struct {
		name        string
		statusCode  int
		contentType string
		body        string
		wantStatus  string
		wantError   bool
	}{
		{name: "valid", statusCode: http.StatusOK, contentType: "application/json", body: `{"title":"ChatGPT","models":[],"categories":[],"versions":[]}`, wantStatus: "valid"},
		{name: "expired", statusCode: http.StatusUnauthorized, contentType: "application/json", body: `{"error":{"message":"Your authentication token has been invalidated.","type":"invalid_request_error","code":"token_invalidated"},"status":401}`, wantStatus: "invalid", wantError: true},
		{name: "auth forbidden", statusCode: http.StatusForbidden, contentType: "application/json", body: `{"error":"invalid access token"}`, wantStatus: "invalid", wantError: true},
		{name: "cloudflare forbidden", statusCode: http.StatusForbidden, contentType: "text/html", body: `<html>blocked</html>`, wantStatus: "blocked", wantError: true},
		{name: "rate limited", statusCode: http.StatusTooManyRequests, contentType: "application/json", body: `{}`, wantStatus: "valid"},
		{name: "upstream failure", statusCode: http.StatusBadGateway, contentType: "text/html", body: `<html>bad gateway</html>`, wantStatus: "probe_failed", wantError: true},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("Authorization") != "Bearer at-test" {
					t.Errorf("authorization header was not sent")
				}
				w.Header().Set("Content-Type", test.contentType)
				w.WriteHeader(test.statusCode)
				_, _ = w.Write([]byte(test.body))
			}))
			defer server.Close()
			sunnyProbeAccessTokenEndpoint = server.URL
			status, err := sunnyProbeAccessToken("at-test", "")
			if status != test.wantStatus || (err != nil) != test.wantError {
				t.Fatalf("probe status=%q err=%v, want status=%q error=%v", status, err, test.wantStatus, test.wantError)
			}
		})
	}
}

func TestSunnySessionListReturnsPersistedHealthStates(t *testing.T) {
	s := newSunnySessionTestServer(t)
	if err := s.db.Model(&SunnySession{}).Where("email = ?", "session@example.com").Updates(map[string]any{
		"access_token_status": "renewal_failed", "access_token_error": "renewal detail",
		"health_check_status": "failed", "health_check_error": "mail detail",
	}).Error; err != nil {
		t.Fatalf("update session state: %v", err)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/sunny/sessions?page=1&page_size=10", nil)
	rec := httptest.NewRecorder()
	s.sunnySessions(rec, req, nil)
	var payload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil || len(payload.Items) != 1 {
		t.Fatalf("decode session list: err=%v body=%s", err, rec.Body.String())
	}
	if payload.Items[0]["access_token_status"] != "renewal_failed" || payload.Items[0]["health_check_status"] != "failed" {
		t.Fatalf("health states were not returned: %#v", payload.Items[0])
	}
	if payload.Items[0]["access_token_error"] != "renewal detail" || payload.Items[0]["health_check_error"] != "mail detail" {
		t.Fatalf("health failure details were not returned: %#v", payload.Items[0])
	}
}

func TestSunnyHealthFailurePersistsAttemptTimeAndReason(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	originalFetch := sunnyFetchOutlookMailSubjects
	defer func() { sunnyFetchOutlookMailSubjects = originalFetch }()
	sunnyFetchOutlookMailSubjects = func(_, _, _ string, _ int, _ string) ([]string, error) { return nil, fmt.Errorf("Graph token expired") }
	task := s.createTask(sunnyHealthTaskType, "sunny", map[string]any{"session_ids": []uint{session.ID}}, 1)
	s.executeSunnyAccountHealthCheckTask(&task, map[string]any{"session_ids": []any{float64(session.ID)}})
	var refreshed SunnySession
	if err := s.db.First(&refreshed, session.ID).Error; err != nil {
		t.Fatalf("reload session: %v", err)
	}
	if refreshed.HealthCheckStatus != "failed" || !strings.Contains(refreshed.HealthCheckError, "Graph token expired") {
		t.Fatalf("unexpected health failure state: %#v", refreshed)
	}
	var mailbox SunnyMailbox
	if err := s.db.Where("email = ?", session.Email).First(&mailbox).Error; err != nil {
		t.Fatalf("reload mailbox: %v", err)
	}
	if mailbox.LastHealthCheckedAt == nil {
		t.Fatalf("health attempt time was not persisted")
	}
}

func TestSunnyHealthTaskDoesNotInspectOrRenewAccessToken(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	originalFetch := sunnyFetchOutlookMailSubjects
	defer func() { sunnyFetchOutlookMailSubjects = originalFetch }()
	sunnyFetchOutlookMailSubjects = func(_, _, _ string, _ int, _ string) ([]string, error) {
		return []string{"Welcome to ChatGPT"}, nil
	}
	healthTask := s.createTask(sunnyHealthTaskType, "sunny", map[string]any{"session_ids": []uint{session.ID}}, 1)
	s.executeSunnyAccountHealthCheckTask(&healthTask, map[string]any{"session_ids": []any{float64(session.ID)}})

	var refreshed SunnySession
	if err := s.db.Where("id = ?", session.ID).First(&refreshed).Error; err != nil {
		t.Fatalf("reload session: %v", err)
	}
	if refreshed.AccessTokenStatus != "unknown" || refreshed.HealthCheckStatus != "alive" {
		t.Fatalf("unexpected session health state: AT=%q health=%q", refreshed.AccessTokenStatus, refreshed.HealthCheckStatus)
	}
	var renewalCount int64
	s.db.Model(&Task{}).Where("type = ?", "sunny_refresh_session").Count(&renewalCount)
	if renewalCount != 0 {
		t.Fatalf("mail health check queued %d AT renewal task(s)", renewalCount)
	}
}

func TestSunnyAccessTokenCheckQueuesRenewalForRejectedToken(t *testing.T) {
	s := newSunnySessionTestServer(t)
	t.Setenv("SUNNY_AT_RENEWAL_CONCURRENCY", "4")
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	originalEndpoint := sunnyProbeAccessTokenEndpoint
	defer func() { sunnyProbeAccessTokenEndpoint = originalEndpoint }()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"error":{"message":"Your authentication token has been invalidated.","type":"invalid_request_error","code":"token_invalidated"},"status":401}`))
	}))
	defer server.Close()
	sunnyProbeAccessTokenEndpoint = server.URL

	checkTask := s.createTask(sunnyAccessTokenCheckTaskType, "sunny", map[string]any{"session_ids": []uint{session.ID}}, 1)
	s.executeSunnyAccessTokenCheckTask(&checkTask, map[string]any{"session_ids": []any{float64(session.ID)}})
	var refreshed SunnySession
	if err := s.db.Where("id = ?", session.ID).First(&refreshed).Error; err != nil {
		t.Fatalf("reload session: %v", err)
	}
	if refreshed.AccessTokenStatus != "invalid" {
		t.Fatalf("AT status=%q, want invalid", refreshed.AccessTokenStatus)
	}
	var renewal Task
	if err := s.db.Where("type = ?", "sunny_refresh_session").First(&renewal).Error; err != nil {
		t.Fatalf("renewal task was not queued: %v", err)
	}
	payload := jsonMap(renewal.PayloadJSON)
	if ids := uintSlice(payload["account_ids"]); len(ids) != 1 || ids[0] != session.AccountID {
		t.Fatalf("unexpected renewal payload: %#v", payload)
	}
	if got := intValue(payload["concurrency"], 0); got != 4 {
		t.Fatalf("renewal concurrency=%d, want 4", got)
	}
}

func TestSunnyAccessTokenCheckSkipsEdgeBlockedTokenWithoutRenewal(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	originalEndpoint := sunnyProbeAccessTokenEndpoint
	defer func() { sunnyProbeAccessTokenEndpoint = originalEndpoint }()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte("<html>Cloudflare challenge</html>"))
	}))
	defer server.Close()
	sunnyProbeAccessTokenEndpoint = server.URL

	checkTask := s.createTask(sunnyAccessTokenCheckTaskType, "sunny", map[string]any{"session_ids": []uint{session.ID}}, 1)
	s.executeSunnyAccessTokenCheckTask(&checkTask, map[string]any{"session_ids": []any{float64(session.ID)}})
	var refreshed SunnySession
	if err := s.db.Where("id = ?", session.ID).First(&refreshed).Error; err != nil {
		t.Fatalf("reload session: %v", err)
	}
	if refreshed.AccessTokenStatus != "probe_blocked" {
		t.Fatalf("AT status=%q, want probe_blocked", refreshed.AccessTokenStatus)
	}
	result := jsonMap(checkTask.ResultJSON)
	if intValue(result["skipped"], 0) != 1 || intValue(result["failed"], 0) != 0 || intValue(result["invalid"], 0) != 0 {
		t.Fatalf("unexpected edge-blocked result: %#v", result)
	}
	var renewalCount int64
	s.db.Model(&Task{}).Where("type = ?", "sunny_refresh_session").Count(&renewalCount)
	if renewalCount != 0 {
		t.Fatalf("edge-blocked probe queued %d renewal task(s)", renewalCount)
	}
}

func TestSunnyScheduledAccessTokenCandidatesRequireAliveHealth(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	candidates, _, err := s.sunnyAccessTokenCandidates(nil, true)
	if err != nil {
		t.Fatalf("scheduled candidates: %v", err)
	}
	if len(candidates) != 0 {
		t.Fatalf("unknown-health account was scheduled: %#v", candidates)
	}
	s.db.Model(&SunnySession{}).Where("id = ?", session.ID).Update("health_check_status", "alive")
	candidates, _, err = s.sunnyAccessTokenCandidates(nil, true)
	if err != nil || len(candidates) != 1 {
		t.Fatalf("alive account was not scheduled: candidates=%#v err=%v", candidates, err)
	}
}

func TestSunnyScheduledTaskDueUsesConfiguredTimeAndFrequency(t *testing.T) {
	location := time.FixedZone("Asia/Shanghai", 8*60*60)
	now := time.Date(2026, 7, 30, 6, 29, 0, 0, location)
	if sunnyScheduledTaskDue(now, "06:30", 24, nil) {
		t.Fatalf("task ran before configured time")
	}
	now = now.Add(time.Minute)
	if !sunnyScheduledTaskDue(now, "06:30", 24, nil) {
		t.Fatalf("task did not run at configured time")
	}
	latest := now.Add(-23 * time.Hour)
	if sunnyScheduledTaskDue(now, "06:30", 24, &latest) {
		t.Fatalf("task ignored configured frequency")
	}
}

func TestSunnyRefreshTaskRejectsEmptySelection(t *testing.T) {
	s := newSunnySessionTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/sunny/tasks/refresh-session", strings.NewReader(`{"session_ids":[]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyTasks(rec, req, []string{"refresh-session"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("empty refresh selection status = %d, body = %s", rec.Code, rec.Body.String())
	}
}

func TestSunnyAcquireRTTaskResolvesSessionSelection(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	if err := s.db.Model(&SunnyAccount{}).Where("id = ?", session.AccountID).Update("phone_number", "+12025550101").Error; err != nil {
		t.Fatalf("mark account phone binding complete: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/sunny/tasks/acquire-rt", strings.NewReader(`{"session_ids":[`+strconv.Itoa(int(session.ID))+`]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyTasks(rec, req, []string{"acquire-rt"})
	if rec.Code != http.StatusOK {
		t.Fatalf("acquire RT status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var task Task
	if err := s.db.Order("created_at desc").First(&task).Error; err != nil {
		t.Fatalf("load task: %v", err)
	}
	payload := jsonMap(task.PayloadJSON)
	if task.Type != "sunny_acquire_rt" || len(uintSlice(payload["account_ids"])) != 1 {
		t.Fatalf("unexpected acquire task: type=%s payload=%#v", task.Type, payload)
	}
}

func TestSunnyAcquireRTTaskRejectsAccountWithoutPhoneBinding(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/sunny/tasks/acquire-rt", strings.NewReader(`{"session_ids":[`+strconv.Itoa(int(session.ID))+`]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyTasks(rec, req, []string{"acquire-rt"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("unbound account status = %d, body = %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "当前账户未接码，请先完成接码后再获取RT") {
		t.Fatalf("unexpected unbound account error: %s", rec.Body.String())
	}
	var taskCount int64
	if err := s.db.Model(&Task{}).Where("type = ?", "sunny_acquire_rt").Count(&taskCount).Error; err != nil {
		t.Fatalf("count acquire RT tasks: %v", err)
	}
	if taskCount != 0 {
		t.Fatalf("unbound account created %d acquire RT tasks", taskCount)
	}
}

func TestSunnyAcquireRTTaskRejectsEmptySelection(t *testing.T) {
	s := newSunnySessionTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/sunny/tasks/acquire-rt", strings.NewReader(`{"session_ids":[]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyTasks(rec, req, []string{"acquire-rt"})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("empty acquire selection status = %d, body = %s", rec.Code, rec.Body.String())
	}
}

func TestSunnySub2ImportTaskResolvesSessionSelection(t *testing.T) {
	s := newSunnySessionTestServer(t)
	s.maintenance = map[string]any{"sub2_import_concurrency": 4}
	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	req := httptest.NewRequest(http.MethodPost, "/api/sunny/tasks/sub2-import", strings.NewReader(`{"session_ids":[`+strconv.Itoa(int(session.ID))+`]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyTasks(rec, req, []string{"sub2-import"})
	if rec.Code != http.StatusOK {
		t.Fatalf("sub2 import status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var task Task
	if err := s.db.Order("created_at desc").First(&task).Error; err != nil {
		t.Fatalf("load sub2 import task: %v", err)
	}
	payload := jsonMap(task.PayloadJSON)
	if task.Type != "sunny_sub2_import" || len(uintSlice(payload["account_ids"])) != 1 || intValue(payload["concurrency"], 0) != 4 {
		t.Fatalf("unexpected sub2 import task: type=%s payload=%#v", task.Type, payload)
	}
}

func TestSunnyAddLSTaskFiltersCompleteLoginSecretsAndUsesSentinelProtocol(t *testing.T) {
	s := newSunnySessionTestServer(t)
	s.maintenance = map[string]any{"add_ls_concurrency": 3}
	var completeSession SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&completeSession).Error; err != nil {
		t.Fatalf("load complete session: %v", err)
	}
	if err := s.db.Model(&SunnyMailbox{}).Where("email = ?", completeSession.Email).Updates(map[string]any{
		"chat_gpt_password": "chatgpt-password", "totp_secret": "JBSWY3DPEHPK3PXP",
	}).Error; err != nil {
		t.Fatalf("complete LS credentials: %v", err)
	}
	now := time.Now()
	incompleteMailbox := SunnyMailbox{Email: "missing-ls@example.com", Password: "mailbox-password", Status: "已注册", Enabled: true, CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&incompleteMailbox).Error; err != nil {
		t.Fatalf("create incomplete mailbox: %v", err)
	}
	incompleteAccount := SunnyAccount{MailboxID: incompleteMailbox.ID, Email: incompleteMailbox.Email, Status: "registered", CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&incompleteAccount).Error; err != nil {
		t.Fatalf("create incomplete account: %v", err)
	}
	incompleteSession := SunnySession{AccountID: incompleteAccount.ID, Email: incompleteAccount.Email, CreatedAt: now, UpdatedAt: now}
	if err := s.db.Create(&incompleteSession).Error; err != nil {
		t.Fatalf("create incomplete session: %v", err)
	}

	body := fmt.Sprintf(`{"session_ids":[%d,%d]}`, completeSession.ID, incompleteSession.ID)
	req := httptest.NewRequest(http.MethodPost, "/api/sunny/tasks/add-ls", strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyTasks(rec, req, []string{"add-ls"})
	if rec.Code != http.StatusOK {
		t.Fatalf("add LS status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var task Task
	if err := s.db.Order("created_at desc").First(&task).Error; err != nil {
		t.Fatalf("load add LS task: %v", err)
	}
	payload := jsonMap(task.PayloadJSON)
	eligible := uintSlice(payload["account_ids"])
	if len(eligible) != 1 || eligible[0] != incompleteAccount.ID {
		t.Fatalf("eligible account IDs = %#v, want [%d]", eligible, incompleteAccount.ID)
	}
	skipped, _ := payload["prefiltered_login_secret_items"].([]any)
	if len(skipped) != 1 {
		t.Fatalf("prefiltered LS items = %#v", payload["prefiltered_login_secret_items"])
	}
	if text(payload["execution_mode"]) != "protocol" || text(payload["protocol_challenge_strategy"]) != "sentinel_protocol" || payload["setup_login_secret"] != true {
		t.Fatalf("unexpected add LS runtime payload: %#v", payload)
	}
	if intValue(payload["concurrency"], 0) != 3 {
		t.Fatalf("add LS concurrency = %v, want 3", payload["concurrency"])
	}
	if task.ProgressTotal != 2 {
		t.Fatalf("task progress total = %d, want 2", task.ProgressTotal)
	}
}

func TestSunnyAccountExportsUseStableNamesAndFormats(t *testing.T) {
	s := newSunnySessionTestServer(t)
	var rows []SunnySession
	s.db.Order("id asc").Find(&rows)

	for _, test := range []struct {
		format      string
		namePattern string
		contentType string
	}{
		{format: "sk", namePattern: `SK-\d{14}-1\.txt`, contentType: "text/plain"},
		{format: "at", namePattern: `AT-\d{14}-1\.txt`, contentType: "text/plain"},
		{format: "sub", namePattern: `SUB-\d{14}-1\.json`, contentType: "application/json"},
	} {
		rec := httptest.NewRecorder()
		s.sunnyExportSessions(rec, rows, test.format)
		if rec.Code != http.StatusOK || !strings.Contains(rec.Header().Get("Content-Type"), test.contentType) {
			t.Fatalf("%s export response: status=%d type=%q", test.format, rec.Code, rec.Header().Get("Content-Type"))
		}
		if !regexp.MustCompile(test.namePattern).MatchString(rec.Header().Get("Content-Disposition")) {
			t.Fatalf("%s export filename = %q", test.format, rec.Header().Get("Content-Disposition"))
		}
	}

	sk := httptest.NewRecorder()
	s.sunnyExportSessions(sk, rows, "sk")
	if strings.TrimSpace(sk.Body.String()) != "session@example.com----mailbox-password----client-id----mailbox-refresh-token" {
		t.Fatalf("unexpected SK export: %q", sk.Body.String())
	}
	at := httptest.NewRecorder()
	s.sunnyExportSessions(at, rows, "at")
	if strings.TrimSpace(at.Body.String()) != "session-access-token" {
		t.Fatalf("unexpected AT export: %q", at.Body.String())
	}
	s.sunnySaveConfig(sunnyCfgSub2API, mergeConfig(defaultSub2APIConfig(), map[string]any{"notes_include_sk": true}))
	sub := httptest.NewRecorder()
	s.sunnyExportSessions(sub, rows, "sub")
	var payload map[string]any
	if err := json.Unmarshal(sub.Body.Bytes(), &payload); err != nil {
		t.Fatalf("decode SUB export: %v", err)
	}
	accounts, _ := payload["accounts"].([]any)
	if len(accounts) != 1 || payload["exported_at"] == "" {
		t.Fatalf("unexpected SUB export: %#v", payload)
	}
	account, _ := accounts[0].(map[string]any)
	credentials, _ := account["credentials"].(map[string]any)
	if account["platform"] != "openai" || account["type"] != "oauth" || credentials["access_token"] != "session-access-token" {
		t.Fatalf("unexpected SUB account: %#v", account)
	}
	if account["notes"] != "邮箱凭证：session@example.com----mailbox-password----client-id----mailbox-refresh-token" || credentials["model_mapping"] == nil || credentials["subscription_expires_at"] == nil {
		t.Fatalf("SUB compatibility fields are missing: %#v", account)
	}
}

func TestExtractSunnyHeaderReadsSubjectOnly(t *testing.T) {
	headerText := "Subject: Access Deactivated\r\nDate: Tue, 21 Jul 2026 06:00:00 +0800\r\n\r\n"
	raw := "* 5 FETCH (BODY[HEADER.FIELDS (SUBJECT DATE)] {" + strconv.Itoa(len(headerText)) + "}\r\n" + headerText + ")\r\nF1 OK FETCH completed\r\n"
	header, ok := extractSunnyHeader(raw, 5, "F1")
	if !ok {
		t.Fatalf("header was not parsed")
	}
	if header.Subject != "Access Deactivated" || header.Date.IsZero() {
		t.Fatalf("unexpected parsed header: %#v", header)
	}
}

func TestSunnyHealthTaskMarksAccountBanned(t *testing.T) {
	s := newSunnySessionTestServer(t)
	previousFetch := sunnyFetchOutlookMailSubjects
	sunnyFetchOutlookMailSubjects = func(email, clientID, refreshToken string, limit int, proxyURL string) ([]string, error) {
		if email != "session@example.com" || limit != 5 {
			t.Fatalf("unexpected health query: email=%s limit=%d", email, limit)
		}
		return []string{"Your account [C-75ROCz5moZsB] has been deactivated"}, nil
	}
	defer func() { sunnyFetchOutlookMailSubjects = previousFetch }()

	var session SunnySession
	if err := s.db.Where("email = ?", "session@example.com").First(&session).Error; err != nil {
		t.Fatalf("load session: %v", err)
	}
	task := s.createTask(sunnyHealthTaskType, "sunny", map[string]any{"session_ids": []uint{session.ID}}, 1)
	s.executeSunnyAccountHealthCheckTask(&task, map[string]any{"session_ids": []uint{session.ID}})

	var mailbox SunnyMailbox
	var account SunnyAccount
	if err := s.db.Where("email = ?", session.Email).First(&mailbox).Error; err != nil {
		t.Fatalf("load mailbox: %v", err)
	}
	if err := s.db.Where("email = ?", session.Email).First(&account).Error; err != nil {
		t.Fatalf("load account: %v", err)
	}
	if mailbox.Status != "已封禁" || account.Status != "已封禁" {
		t.Fatalf("banned status not synchronized: mailbox=%q account=%q", mailbox.Status, account.Status)
	}
	if mailbox.LastHealthCheckedAt == nil || account.LastHealthCheckedAt == nil {
		t.Fatalf("last health timestamps were not persisted")
	}
	if mailbox.StatusChangedAt == nil || account.StatusChangedAt == nil {
		t.Fatalf("status change timestamps were not persisted")
	}
	if err := s.db.First(&task, "id = ?", task.ID).Error; err != nil {
		t.Fatalf("reload task: %v", err)
	}
	result := jsonMap(task.ResultJSON)
	if task.Status != TaskSucceeded || intValue(result["banned"], 0) != 1 || intValue(result["alive"], 0) != 0 {
		t.Fatalf("unexpected health task result: status=%s result=%#v", task.Status, result)
	}
}

func TestSunnyHealthTaskAliveDoesNotChangeEditOrStatusTime(t *testing.T) {
	s := newSunnySessionTestServer(t)
	previousFetch := sunnyFetchOutlookMailSubjects
	previousEndpoint := sunnyProbeAccessTokenEndpoint
	sunnyFetchOutlookMailSubjects = func(email, clientID, refreshToken string, limit int, proxyURL string) ([]string, error) {
		return []string{
			"OpenAI ChatGPT - 規定違反と無効化についての警告 [C-gzvRqiPNpDSe]\n" +
				"規定違反が繰り返される場合は、サービスへのアクセスを無効にする可能性があります。",
		}, nil
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()
	sunnyProbeAccessTokenEndpoint = server.URL
	defer func() {
		sunnyFetchOutlookMailSubjects = previousFetch
		sunnyProbeAccessTokenEndpoint = previousEndpoint
	}()

	var session SunnySession
	var beforeMailbox SunnyMailbox
	var beforeAccount SunnyAccount
	s.db.Where("email = ?", "session@example.com").First(&session)
	s.db.Where("email = ?", session.Email).First(&beforeMailbox)
	s.db.Where("email = ?", session.Email).First(&beforeAccount)
	statusTime := beforeMailbox.UpdatedAt.Add(-time.Hour)
	s.db.Model(&SunnyMailbox{}).Where("id = ?", beforeMailbox.ID).UpdateColumn("status_changed_at", statusTime)
	s.db.Model(&SunnyAccount{}).Where("id = ?", beforeAccount.ID).UpdateColumn("status_changed_at", statusTime)

	task := s.createTask(sunnyHealthTaskType, "sunny", map[string]any{"session_ids": []uint{session.ID}}, 1)
	s.executeSunnyAccountHealthCheckTask(&task, map[string]any{"session_ids": []uint{session.ID}})

	var afterMailbox SunnyMailbox
	var afterAccount SunnyAccount
	s.db.First(&afterMailbox, beforeMailbox.ID)
	s.db.First(&afterAccount, beforeAccount.ID)
	if !afterMailbox.UpdatedAt.Equal(beforeMailbox.UpdatedAt) || !afterAccount.UpdatedAt.Equal(beforeAccount.UpdatedAt) {
		t.Fatalf("alive health check changed edit time: mailbox=%v/%v account=%v/%v", beforeMailbox.UpdatedAt, afterMailbox.UpdatedAt, beforeAccount.UpdatedAt, afterAccount.UpdatedAt)
	}
	if afterMailbox.StatusChangedAt == nil || !afterMailbox.StatusChangedAt.Equal(statusTime) || afterAccount.StatusChangedAt == nil || !afterAccount.StatusChangedAt.Equal(statusTime) {
		t.Fatalf("alive health check changed status time: mailbox=%v account=%v", afterMailbox.StatusChangedAt, afterAccount.StatusChangedAt)
	}
	if afterMailbox.LastHealthCheckedAt == nil || afterAccount.LastHealthCheckedAt == nil {
		t.Fatalf("alive health check did not persist health time")
	}
}

func TestSunnyMaintenanceConfigAppliesImmediatelyAndPersists(t *testing.T) {
	s := newSunnySessionTestServer(t)
	s.maintenance = defaultSunnyMaintenanceConfigForCPU(4)

	body := strings.NewReader(`{"health_enabled":true,"health_time":"07:15","health_frequency_hours":12,"health_concurrency":5,"at_enabled":true,"at_time":"07:45","at_frequency_hours":6,"at_concurrency":2,"rebind_concurrency":2,"sub2_import_concurrency":4,"trial_concurrency":5,"checkout_probe_concurrency":4,"payment_probe_concurrency":2,"payment_country_concurrency":2,"add_ls_concurrency":3,"subscription_concurrency":3}`)
	req := httptest.NewRequest(http.MethodPut, "/sunny/maintenance-config", body)
	recorder := httptest.NewRecorder()
	s.sunnyMaintenanceConfigHandler(recorder, req)
	if recorder.Code != http.StatusOK {
		t.Fatalf("save maintenance config: status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var response map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	if boolValue(response["restart_required"], true) || !boolValue(response["effective_immediately"], false) {
		t.Fatalf("save response did not apply immediately: %#v", response)
	}
	if got := text(s.sunnyMaintenanceSnapshot()["health_time"]); got != "07:15" {
		t.Fatalf("runtime config was not updated: %s", got)
	}
	if got := s.sunnyHealthCheckConcurrency(); got != 5 {
		t.Fatalf("runtime health concurrency = %d, want 5", got)
	}
	stored := s.sunnyGetConfig(sunnyCfgMaintenance, defaultSunnyMaintenanceConfig())
	if text(stored["health_time"]) != "07:15" || intValue(stored["at_frequency_hours"], 0) != 6 || intValue(stored["rebind_concurrency"], 0) != 2 {
		t.Fatalf("stored maintenance config mismatch: %#v", stored)
	}
}

func TestSunnyMaintenanceCPUDefaultsUseResourceTiers(t *testing.T) {
	config := defaultSunnyMaintenanceConfigForCPU(4)
	want := map[string]int{
		"rebind_concurrency": 3, "sub2_import_concurrency": 4, "trial_concurrency": 4,
		"checkout_probe_concurrency": 4, "payment_probe_concurrency": 2,
		"payment_country_concurrency": 2, "add_ls_concurrency": 3,
		"at_concurrency": 2, "health_concurrency": 4, "subscription_concurrency": 3,
	}
	for key, expected := range want {
		if got := intValue(config[key], 0); got != expected {
			t.Fatalf("%s default = %d, want %d", key, got, expected)
		}
	}
}
