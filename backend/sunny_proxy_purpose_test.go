package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"github.com/glebarez/sqlite"
	"gorm.io/gorm"
)

func TestNormalizeSunnyProxyPurposes(t *testing.T) {
	got := normalizeSunnyProxyPurposes([]any{"trial", "register", "checkout", "payment", "unknown"})
	if strings.Join(got, ",") != "commerce,register,payment_probe" {
		t.Fatalf("purposes=%v", got)
	}
}

func TestSunnyProxyEmptyPurposeIsPersistedAndExcludedFromTasks(t *testing.T) {
	s := newSunnySessionTestServer(t)
	s.sunnySaveConfig(sunnyCfgProxy, mergeConfig(defaultProxyConfig(), map[string]any{"proxy_enabled": true}))
	unused := SunnyProxy{
		Address: "http://unused.example:8080", PurposeTags: sunnyProxyPurposeRegister,
		Status: "enabled", Enabled: true, LastCheckOK: true,
	}
	register := SunnyProxy{
		Address: "http://register.example:8080", PurposeTags: sunnyProxyPurposeRegister,
		Status: "enabled", Enabled: true, LastCheckOK: true,
	}
	if err := s.db.Create(&[]*SunnyProxy{&unused, &register}).Error; err != nil {
		t.Fatalf("create proxies: %v", err)
	}

	id := strconv.FormatUint(uint64(unused.ID), 10)
	req := httptest.NewRequest(http.MethodPut, "/api/sunny/proxy-config/pool/"+id, strings.NewReader(`{"purpose_tags":[]}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyProxyPool(rec, req, []string{id})
	if rec.Code != http.StatusOK {
		t.Fatalf("clear purpose status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var response map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	tags, ok := response["purpose_tags"].([]any)
	if !ok || len(tags) != 0 {
		t.Fatalf("response purpose_tags = %#v", response["purpose_tags"])
	}
	if err := s.db.First(&unused, unused.ID).Error; err != nil {
		t.Fatalf("reload unused proxy: %v", err)
	}
	if unused.PurposeTags != "" {
		t.Fatalf("stored purpose_tags = %q", unused.PurposeTags)
	}

	snapshot := s.sunnyTaskProxySnapshot(map[string]any{})
	pool, ok := snapshot["proxy_pool"].([]string)
	if !ok || len(pool) != 1 || pool[0] != register.Address {
		t.Fatalf("task proxy_pool = %#v", snapshot["proxy_pool"])
	}
	ids, ok := snapshot["proxy_ids"].([]uint)
	if !ok || len(ids) != 1 || ids[0] != register.ID {
		t.Fatalf("task proxy_ids = %#v", snapshot["proxy_ids"])
	}
}

func TestSunnyRegisterProxyFallbackRequiresExplicitConfirmation(t *testing.T) {
	s := newSunnySessionTestServer(t)
	s.sunnySaveConfig(sunnyCfgProxy, mergeConfig(defaultProxyConfig(), map[string]any{"proxy_enabled": true}))
	proxy := SunnyProxy{
		Address: "http://commerce-only.example:8080", PurposeTags: sunnyProxyPurposeCommerce,
		Status: "enabled", Enabled: true, LastCheckOK: true,
	}
	if err := s.db.Create(&proxy).Error; err != nil {
		t.Fatalf("create proxy: %v", err)
	}

	readiness := s.sunnyRegisterProxyReadiness()
	if !boolValue(readiness["requires_confirmation"], false) || boolValue(readiness["usable"], true) {
		t.Fatalf("unexpected readiness: %#v", readiness)
	}
	if err := s.sunnyValidateProxyForRegisterTask(false); err == nil {
		t.Fatal("registration without fallback confirmation should be rejected")
	}
	if err := s.sunnyValidateProxyForRegisterTask(true); err != nil {
		t.Fatalf("confirmed system fallback rejected: %v", err)
	}

	snapshot := s.sunnyTaskProxySnapshot(map[string]any{"allow_system_proxy_fallback": true})
	if boolValue(snapshot["proxy_enabled"], true) {
		t.Fatalf("confirmed snapshot kept proxy pool enabled: %#v", snapshot)
	}
	if !boolValue(snapshot["proxy_pool_fallback_confirmed"], false) {
		t.Fatalf("confirmed snapshot missing fallback marker: %#v", snapshot)
	}
	if text(snapshot["register_proxy"]) != "" || text(snapshot["proxy"]) != "" {
		t.Fatalf("confirmed snapshot retained registration proxy: %#v", snapshot)
	}
}

func TestSunnyRegisterProxyReadinessAcceptsEnabledRegisterProxy(t *testing.T) {
	s := newSunnySessionTestServer(t)
	s.sunnySaveConfig(sunnyCfgProxy, mergeConfig(defaultProxyConfig(), map[string]any{"proxy_enabled": true}))
	proxy := SunnyProxy{
		Address: "http://register-ready.example:8080", PurposeTags: sunnyProxyPurposeRegister,
		Status: "enabled", Enabled: true, LastCheckOK: true,
	}
	if err := s.db.Create(&proxy).Error; err != nil {
		t.Fatalf("create proxy: %v", err)
	}

	readiness := s.sunnyRegisterProxyReadiness()
	if !boolValue(readiness["usable"], false) || boolValue(readiness["requires_confirmation"], true) {
		t.Fatalf("unexpected readiness: %#v", readiness)
	}
	if err := s.sunnyValidateProxyForRegisterTask(false); err != nil {
		t.Fatalf("enabled register proxy rejected: %v", err)
	}
}

func TestSunnyProxyCreatePreservesExplicitEmptyPurpose(t *testing.T) {
	s := newSunnySessionTestServer(t)
	req := httptest.NewRequest(http.MethodPost, "/api/sunny/proxy-config/pool", strings.NewReader(`{
		"addresses":["http://unused-new.example:8080"],
		"country":"US",
		"purpose_tags":[],
		"status":"停用",
		"enabled":false
	}`))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	s.sunnyProxyPool(rec, req, nil)
	if rec.Code != http.StatusOK {
		t.Fatalf("create empty-purpose proxy status = %d, body = %s", rec.Code, rec.Body.String())
	}
	var response map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
		t.Fatalf("decode response: %v", err)
	}
	tags, ok := response["purpose_tags"].([]any)
	if !ok || len(tags) != 0 {
		t.Fatalf("response purpose_tags = %#v", response["purpose_tags"])
	}
	var proxy SunnyProxy
	if err := s.db.First(&proxy, "address = ?", "http://unused-new.example:8080").Error; err != nil {
		t.Fatalf("reload proxy: %v", err)
	}
	if proxy.PurposeTags != "" {
		t.Fatalf("stored purpose_tags = %q", proxy.PurposeTags)
	}
}

func TestSunnyCommerceProxyURLPrefersCheckoutCountryAndPurpose(t *testing.T) {
	db, err := gorm.Open(sqlite.Open("file:"+strings.ReplaceAll(t.Name(), "/", "-")+"?mode=memory&cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if err := db.AutoMigrate(&SunnyProxy{}); err != nil {
		t.Fatal(err)
	}
	rows := []SunnyProxy{
		{Address: "http://register.example:8080", Country: "US", PurposeTags: "register", Status: "enabled", Enabled: true, LastCheckOK: true},
		{Address: "http://commerce-de.example:8080", Country: "DE", PurposeTags: "commerce", Status: "enabled", Enabled: true, LastCheckOK: true},
		{Address: "http://commerce-us.example:8080", Country: "US", PurposeTags: "commerce", Status: "enabled", Enabled: true, LastCheckOK: true},
	}
	if err := db.Create(&rows).Error; err != nil {
		t.Fatal(err)
	}
	t.Setenv("SUNNY_CHECKOUT_COUNTRY", "US")
	server := &Server{db: db}
	if got := server.sunnyCommerceProxyURL("account@example.com"); got != "http://commerce-us.example:8080" {
		t.Fatalf("proxy=%q", got)
	}
	if got := server.sunnyRegisterProxyURL("account@example.com"); got != "http://register.example:8080" {
		t.Fatalf("register proxy=%q", got)
	}
}
