package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html"
	"io"
	"mime"
	"mime/multipart"
	"mime/quotedprintable"
	"net"
	"net/http"
	"net/mail"
	"net/textproto"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/text/encoding/htmlindex"
	"gorm.io/gorm"
)

const (
	sunnyCfgProxy    = "proxy"
	sunnyCfgSub2API  = "sub2api"
	sunnyCfgPhone    = "phone"
	sunnyCfgMailbox  = "mailbox"
	defaultGroupName = "默认分组"
	fireFoxAPIURL    = "https://www.firefox.fun/yhapi.ashx"
)

var sunnyMailboxStatuses = []string{"未注册", "已注册", "已接码", "已反代", "已封禁", "需二验", "登录刷新", "失败"}

func (s *Server) handleSunny(w http.ResponseWriter, r *http.Request, rest string) {
	rest = strings.Trim(rest, "/")
	if rest == "" {
		writeJSON(w, 200, map[string]any{"ok": true, "module": "sunny"})
		return
	}
	parts := strings.Split(rest, "/")
	switch parts[0] {
	case "workbench":
		if len(parts) == 2 && parts[1] == "accounts" && r.Method == http.MethodGet {
			s.sunnyListAccounts(w, r)
			return
		}
	case "mailbox-groups":
		s.sunnyMailboxGroups(w, r, parts[1:])
		return
	case "mailboxes":
		s.sunnyMailboxes(w, r, parts[1:])
		return
	case "remail":
		s.remailConfigHandler(w, r, parts[1:])
		return
	case "domain-mail":
		s.domainMailboxConfigHandler(w, r, parts[1:])
		return
	case "phones":
		s.sunnyPhones(w, r, parts[1:])
		return
	case "proxy-config":
		s.sunnyProxyConfig(w, r, parts[1:])
		return
	case "sub2api-config":
		s.sunnySub2APIConfig(w, r)
		return
	case "sub2api":
		s.sunnySub2API(w, r, parts[1:])
		return
	case "sessions":
		s.sunnySessions(w, r, parts[1:])
		return
	case "maintenance-config":
		s.sunnyMaintenanceConfigHandler(w, r)
		return
	case "tasks":
		s.sunnyTasks(w, r, parts[1:])
		return
	case "checkout":
		s.sunnyCheckout(w, r, parts[1:])
		return
	case "import-state":
		if r.Method == http.MethodPost {
			s.sunnyImportState(w, r)
			return
		}
	}
	writeError(w, 404, "not found")
}

func (s *Server) sunnyEnsureDefaultGroup() uint {
	var g SunnyMailboxGroup
	if err := s.db.First(&g, "name = ?", defaultGroupName).Error; err == nil {
		return g.ID
	}
	g = SunnyMailboxGroup{Name: defaultGroupName}
	s.db.Create(&g)
	return g.ID
}

func (s *Server) sunnyMailboxGroups(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 0 && r.Method == http.MethodGet {
		s.sunnyEnsureDefaultGroup()
		var rows []SunnyMailboxGroup
		s.db.Find(&rows)
		sort.SliceStable(rows, func(i, j int) bool {
			iDefault := rows[i].Name == defaultGroupName
			jDefault := rows[j].Name == defaultGroupName
			if iDefault != jDefault {
				return iDefault
			}
			if !rows[i].CreatedAt.Equal(rows[j].CreatedAt) {
				return rows[i].CreatedAt.After(rows[j].CreatedAt)
			}
			return rows[i].ID > rows[j].ID
		})
		var countRows []struct {
			GroupID uint  `gorm:"column:group_id"`
			Count   int64 `gorm:"column:mailbox_count"`
		}
		s.db.Model(&SunnyMailbox{}).
			Select("group_id, COUNT(*) AS mailbox_count").
			Group("group_id").
			Scan(&countRows)
		counts := map[uint]int64{}
		for _, row := range countRows {
			counts[row.GroupID] = row.Count
		}
		items := make([]map[string]any, 0, len(rows))
		for _, row := range rows {
			items = append(items, map[string]any{
				"id": row.ID, "name": row.Name, "description": row.Description,
				"mailbox_count": counts[row.ID], "created_at": formatTime(row.CreatedAt), "updated_at": formatTime(row.UpdatedAt),
			})
		}
		writeJSON(w, 200, map[string]any{"items": items})
		return
	}
	if len(parts) == 0 && r.Method == http.MethodPost {
		body, _ := parseBody(r)
		name := fallback(text(body["name"]), defaultGroupName)
		g := SunnyMailboxGroup{Name: name, Description: text(body["description"])}
		if err := s.db.Create(&g).Error; err != nil {
			writeError(w, 400, err.Error())
			return
		}
		writeJSON(w, 200, map[string]any{
			"id": g.ID, "name": g.Name, "description": g.Description,
			"mailbox_count": 0, "created_at": formatTime(g.CreatedAt), "updated_at": formatTime(g.UpdatedAt),
		})
		return
	}
	if len(parts) == 1 {
		id := uint(intValue(parts[0], 0))
		if id == 0 {
			writeError(w, 400, "invalid group id")
			return
		}
		if r.Method == http.MethodPut {
			body, _ := parseBody(r)
			var g SunnyMailboxGroup
			if s.db.First(&g, id).Error != nil {
				writeError(w, 404, "group not found")
				return
			}
			if name := strings.TrimSpace(text(body["name"])); name != "" {
				g.Name = name
			}
			if description, ok := body["description"]; ok {
				g.Description = text(description)
			}
			if err := s.db.Save(&g).Error; err != nil {
				writeJSON(w, http.StatusConflict, map[string]any{"error": "mailbox_group_name_conflict", "detail": "邮箱分组名称已存在"})
				return
			}
			var mailboxCount int64
			s.db.Model(&SunnyMailbox{}).Where("group_id = ?", id).Count(&mailboxCount)
			writeJSON(w, 200, map[string]any{
				"id": g.ID, "name": g.Name, "description": g.Description,
				"mailbox_count": mailboxCount, "created_at": formatTime(g.CreatedAt), "updated_at": formatTime(g.UpdatedAt),
			})
			return
		}
		if r.Method == http.MethodDelete {
			var g SunnyMailboxGroup
			if s.db.First(&g, id).Error != nil {
				writeError(w, http.StatusNotFound, "group not found")
				return
			}
			if g.Name == defaultGroupName {
				writeJSON(w, http.StatusConflict, map[string]any{"error": "default_mailbox_group", "detail": "默认分组不能删除"})
				return
			}
			var mailboxCount int64
			s.db.Model(&SunnyMailbox{}).Where("group_id = ?", id).Count(&mailboxCount)
			if mailboxCount > 0 {
				writeJSON(w, http.StatusConflict, map[string]any{
					"error": "mailbox_group_not_empty", "detail": "该邮箱分组下存在邮箱账户，请移除后再删除分组",
					"mailbox_count": mailboxCount,
				})
				return
			}
			if err := s.db.Delete(&g).Error; err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
			writeJSON(w, 200, map[string]any{"ok": true})
			return
		}
	}
	writeError(w, 404, "not found")
}

func serializeSunnyMailbox(m SunnyMailbox, groups map[uint]string, planType ...string) map[string]any {
	status := m.Status
	if status == "" {
		status = "unused"
	}
	plan := "-"
	if len(planType) > 0 && strings.TrimSpace(planType[0]) != "" {
		plan = planType[0]
	}
	accessToken := ""
	if len(planType) > 1 {
		accessToken = planType[1]
	}
	accountID := uint(0)
	if len(planType) > 2 {
		accountID = uint(intValue(planType[2], 0))
	}
	trialEligibility := normalizeSunnyTrialEligibility(m.TrialEligibility)
	if len(planType) > 3 && strings.TrimSpace(planType[3]) != "" {
		trialEligibility = normalizeSunnyTrialEligibility(planType[3])
	}
	return map[string]any{
		"id": m.ID, "account_id": accountID, "group_id": m.GroupID, "group_name": groups[m.GroupID], "email": m.Email, "rebind_email": m.RebindEmail, "rebind_mailbox_api": m.RebindMailboxAPI,
		"mailbox_type": normalizeSunnyMailboxType(m.MailboxType), "mailbox_channel": normalizeSunnyMailboxChannel(m.MailboxType, m.MailboxChannel), "access_key": m.AccessKey,
		"password": m.Password, "chatgpt_password": m.ChatGPTPassword, "totp_secret": m.TOTPSecret, "client_id": m.ClientID, "refresh_token": m.RefreshToken, "openai_rt": m.OpenAIRT, "access_token": accessToken,
		"has_chatgpt_password": strings.TrimSpace(m.ChatGPTPassword) != "", "has_totp_secret": strings.TrimSpace(m.TOTPSecret) != "",
		"has_login_secret": sunnyLoginSecretLine(m) != "", "has_secret_key": sunnyMailboxCredentialLine(m) != "", "chatgpt_password_preview": sunnyCredentialPreview(m.ChatGPTPassword), "totp_secret_preview": sunnyCredentialPreview(m.TOTPSecret),
		"raw": m.Raw, "account_type": fallback(m.AccountType, "free"), "plan_type": plan, "trial_eligibility": trialEligibility, "status": status, "enabled": m.Enabled,
		"chatgpt_register_traffic_bytes": m.ChatGPTRegisterTrafficBytes, "proxy_traffic_bytes": m.ProxyTrafficBytes,
		"last_error": m.LastError, "latest_mail": jsonMap(m.LatestMailJSON),
		"last_mail_at":  nullableTime(m.LastMailAt.Valid, m.LastMailAt.Time),
		"registered_at": nullableTime(m.RegisteredAt.Valid, m.RegisteredAt.Time),
		"created_at":    formatTime(m.CreatedAt), "updated_at": formatTime(m.UpdatedAt),
		"status_changed_at": nullableTime(m.StatusChangedAt != nil, pointerTime(m.StatusChangedAt)),
		"trial_checked_at":  nullableTime(m.TrialCheckedAt != nil, pointerTime(m.TrialCheckedAt)),
	}
}

func pointerTime(value *time.Time) time.Time {
	if value == nil {
		return time.Time{}
	}
	return *value
}

func serializeSunnyMailboxList(m SunnyMailbox, groups map[uint]string, plan, accessToken string, accountID uint, trialEligibility string, summary bool) map[string]any {
	item := serializeSunnyMailbox(m, groups, plan, accessToken, strconv.FormatUint(uint64(accountID), 10), trialEligibility)
	if !summary {
		return item
	}
	item["has_openai_rt"] = strings.TrimSpace(m.OpenAIRT) != ""
	item["has_access_token"] = strings.TrimSpace(accessToken) != ""
	for _, key := range []string{"password", "chatgpt_password", "totp_secret", "client_id", "refresh_token", "access_key", "rebind_mailbox_api", "openai_rt", "access_token", "raw", "last_error", "latest_mail", "last_mail_at"} {
		delete(item, key)
	}
	return item
}

func normalizeSunnyPlanType(v string) string {
	v = strings.TrimSpace(strings.ToLower(v))
	v = strings.Trim(v, "\"'")
	switch v {
	case "", "unknown", "null", "none":
		return ""
	case "chatgptplus", "chatgpt_plus", "plus_user", "paid":
		return "plus"
	case "chatgptfree", "free_user":
		return "free"
	default:
		return v
	}
}

func sunnyPlanTypeFromSessionJSON(raw string) string {
	data := jsonMap(raw)
	if len(data) == 0 {
		return ""
	}
	if account, ok := data["account"].(map[string]any); ok {
		if plan := normalizeSunnyPlanType(firstText(account["planType"], account["plan_type"], account["plan"], account["type"])); plan != "" {
			return plan
		}
	}
	if user, ok := data["user"].(map[string]any); ok {
		if account, ok := user["account"].(map[string]any); ok {
			if plan := normalizeSunnyPlanType(firstText(account["planType"], account["plan_type"], account["plan"], account["type"])); plan != "" {
				return plan
			}
		}
	}
	return normalizeSunnyPlanType(firstText(data["planType"], data["plan_type"], data["plan"], data["account_type"]))
}

func sunnyAccessTokenFromSessionJSON(raw string) string {
	data := jsonMap(raw)
	if len(data) == 0 {
		return ""
	}
	if token := firstText(data["accessToken"], data["access_token"], data["token"]); token != "" {
		return token
	}
	if auth, ok := data["auth"].(map[string]any); ok {
		if token := firstText(auth["accessToken"], auth["access_token"]); token != "" {
			return token
		}
	}
	return ""
}

func sunnyEmailKey(email string) string {
	return strings.ToLower(strings.TrimSpace(email))
}

// Keep one session row per normalized mailbox identity in account-management
// reads. Older deployments could contain duplicate rows created before the
// case-insensitive unique index was installed; the newest row is authoritative.
func sunnyUniqueSessionIdentityScope(query *gorm.DB) *gorm.DB {
	return query.Where(`(
		TRIM(sunny_sessions.email) = '' OR sunny_sessions.id IN (
			SELECT id FROM (
				SELECT id, ROW_NUMBER() OVER (
					PARTITION BY LOWER(TRIM(email))
					ORDER BY updated_at DESC, id DESC
				) AS sunny_identity_rank
				FROM sunny_sessions
				WHERE TRIM(email) <> ''
			) AS sunny_ranked
			WHERE sunny_identity_rank = 1
		)
	)`)
}

func normalizeSunnyEditableEmail(value string) (string, error) {
	email := strings.TrimSpace(value)
	if email == "" {
		return "", fmt.Errorf("邮箱地址不能为空")
	}
	if strings.ContainsAny(email, "\r\n") {
		return "", fmt.Errorf("邮箱地址格式无效")
	}
	parsed, err := mail.ParseAddress(email)
	if err != nil || parsed.Address != email || !strings.Contains(email, "@") {
		return "", fmt.Errorf("邮箱地址格式无效")
	}
	return email, nil
}

func sunnyEmailRenameConflict(tx *gorm.DB, email string) error {
	key := sunnyEmailKey(email)
	var mailboxCount int64
	if err := tx.Model(&SunnyMailbox{}).Where("LOWER(email) = ? OR LOWER(rebind_email) = ?", key, key).Count(&mailboxCount).Error; err != nil {
		return err
	}
	if mailboxCount > 0 {
		return fmt.Errorf("邮箱地址已被其他邮箱、账户或会话使用")
	}
	for _, model := range []any{&SunnyAccount{}, &SunnySession{}} {
		var count int64
		if err := tx.Model(model).Where("LOWER(email) = ?", key).Count(&count).Error; err != nil {
			return err
		}
		if count > 0 {
			return fmt.Errorf("邮箱地址已被其他邮箱、账户或会话使用")
		}
	}
	return nil
}

func sunnyMailboxRawForEmail(mailbox SunnyMailbox, email string) string {
	email = strings.TrimSpace(email)
	mailboxType := normalizeSunnyMailboxType(mailbox.MailboxType)
	mailboxChannel := normalizeSunnyMailboxChannel(mailbox.MailboxType, mailbox.MailboxChannel)
	if mailboxType == "remail" {
		if strings.TrimSpace(mailbox.AccessKey) != "" {
			return strings.Join([]string{email, strings.TrimSpace(mailbox.AccessKey)}, "----")
		}
	} else if mailboxType == "domain" {
		if strings.TrimSpace(mailbox.AccessKey) != "" {
			credentialEmail := email
			if strings.TrimSpace(mailbox.RebindEmail) != "" {
				credentialEmail = strings.TrimSpace(mailbox.RebindEmail)
			}
			accessKey := strings.TrimSpace(mailbox.AccessKey)
			if strings.TrimSpace(mailbox.RebindMailboxAPI) != "" {
				accessKey = strings.TrimSpace(mailbox.RebindMailboxAPI)
			}
			return strings.Join([]string{credentialEmail, accessKey}, "----")
		}
	} else if mailboxType == "apple" {
		if mailboxChannel == "url_api" || mailboxChannel == "xbovo" {
			if strings.TrimSpace(mailbox.AccessKey) != "" {
				return strings.Join([]string{email, strings.TrimSpace(mailbox.AccessKey)}, "----")
			}
		}
	} else if strings.TrimSpace(mailbox.Password) != "" && strings.TrimSpace(mailbox.ClientID) != "" && strings.TrimSpace(mailbox.RefreshToken) != "" {
		return sunnyMicrosoftRaw(email, mailbox.Password, mailbox.ClientID, mailbox.RefreshToken)
	}
	raw := strings.TrimSpace(mailbox.Raw)
	if raw == "" {
		return ""
	}
	parts := strings.Split(raw, "----")
	if len(parts) > 0 {
		parts[0] = email
	}
	return strings.Join(parts, "----")
}

func sunnyMailboxStatusLooksRegistered(status string) bool {
	status = strings.TrimSpace(strings.ToLower(status))
	if status == "" || status == "unused" || status == "unregistered" || status == "未注册" {
		return false
	}
	return true
}
func (s *Server) sunnySessionPlanTypesByEmail(emails []string) map[string]string {
	out := map[string]string{}
	if len(emails) == 0 {
		return out
	}
	var rows []SunnySession
	s.db.Select("email", "session_json").Where("email IN ?", emails).Find(&rows)
	for _, sess := range rows {
		plan := sunnyPlanTypeFromSessionJSON(sess.SessionJSON)
		if plan == "" {
			plan = "free"
		}
		out[sunnyEmailKey(sess.Email)] = plan
	}
	return out
}

func (s *Server) sunnyAccountPresenceByEmail(emails []string) map[string]bool {
	out := map[string]bool{}
	if len(emails) == 0 {
		return out
	}
	var rows []SunnyAccount
	s.db.Select("email").Where("email IN ?", emails).Find(&rows)
	for _, a := range rows {
		out[sunnyEmailKey(a.Email)] = true
	}
	return out
}

func (s *Server) sunnyMailboxAccessTokensByEmail(emails []string) map[string]string {
	out := map[string]string{}
	if len(emails) == 0 {
		return out
	}
	var accounts []SunnyAccount
	s.db.Select("email", "access_token").Where("email IN ?", emails).Find(&accounts)
	for _, a := range accounts {
		if strings.TrimSpace(a.AccessToken) != "" {
			key := sunnyEmailKey(a.Email)
			out[key] = sunnyPreferredAccessToken(out[key], a.AccessToken)
		}
	}
	var sessions []SunnySession
	s.db.Select("email", "access_token", "session_json").Where("email IN ?", emails).Find(&sessions)
	for _, sess := range sessions {
		key := sunnyEmailKey(sess.Email)
		if token := sunnyPreferredAccessToken(sess.AccessToken, sunnyAccessTokenFromSessionJSON(sess.SessionJSON)); token != "" {
			// Sessions contain the latest login result; keep mailbox and account
			// tables aligned when the legacy account copy is stale.
			out[key] = sunnyPreferredAccessToken(token, out[key])
		}
	}
	return out
}

func sunnyMailboxCredentialEmails(mailbox SunnyMailbox) []string {
	emails := []string{}
	for _, value := range []string{mailbox.Email, mailbox.RebindEmail} {
		value = strings.TrimSpace(value)
		if value == "" {
			continue
		}
		seen := false
		for _, existing := range emails {
			if sunnyEmailKey(existing) == sunnyEmailKey(value) {
				seen = true
				break
			}
		}
		if !seen {
			emails = append(emails, value)
		}
	}
	return emails
}

func (s *Server) sunnyMailboxAccessToken(mailbox SunnyMailbox) string {
	linked := s.sunnyMailboxLinkedDataByEmail(sunnyMailboxCredentialEmails(mailbox))
	return sunnyMailboxAccessTokenFromLinked(mailbox, linked)
}

func sunnyMailboxAccessTokenFromLinked(mailbox SunnyMailbox, linked sunnyMailboxLinkedData) string {
	for _, email := range sunnyMailboxCredentialEmails(mailbox) {
		if token := linked.accessTokens[sunnyEmailKey(email)]; token != "" {
			return token
		}
	}
	return ""
}

func sunnySaveMailboxAccessToken(tx *gorm.DB, mailbox SunnyMailbox, groupName, accessToken string) error {
	emails := sunnyMailboxCredentialEmails(mailbox)
	if strings.TrimSpace(accessToken) == "" {
		var accounts []SunnyAccount
		accountQuery := tx.Where("mailbox_id = ?", mailbox.ID).Or("email IN ?", emails)
		if err := accountQuery.Find(&accounts).Error; err != nil {
			return err
		}
		accountIDs := make([]uint, 0, len(accounts))
		for _, account := range accounts {
			accountIDs = append(accountIDs, account.ID)
		}
		if err := tx.Model(&SunnyAccount{}).Where("mailbox_id = ?", mailbox.ID).Or("email IN ?", emails).Updates(map[string]any{"access_token": ""}).Error; err != nil {
			return err
		}
		sessionQuery := tx.Model(&SunnySession{}).Where("email IN ?", emails)
		if len(accountIDs) > 0 {
			sessionQuery = sessionQuery.Or("account_id IN ?", accountIDs)
		}
		return sessionQuery.Updates(map[string]any{
			"access_token": "", "access_token_status": "unknown", "access_token_error": "",
			"access_token_checked_at": nil, "expires_at": nil,
		}).Error
	}
	var account SunnyAccount
	accountQuery := tx.Where("mailbox_id = ?", mailbox.ID)
	if err := accountQuery.First(&account).Error; err != nil {
		accountQuery = tx.Where("email IN ?", emails)
		_ = accountQuery.First(&account).Error
	}
	if account.ID == 0 {
		account = SunnyAccount{
			Email: mailbox.Email, MailboxID: mailbox.ID, GroupName: groupName, Status: mailbox.Status,
			AccountType: mailbox.AccountType, TrialEligibility: mailbox.TrialEligibility, TrialCheckError: mailbox.TrialCheckError,
			TrialCheckedAt: mailbox.TrialCheckedAt, OpenAIRT: mailbox.OpenAIRT, MetadataJSON: "{}",
		}
		if err := tx.Create(&account).Error; err != nil {
			return err
		}
	}
	if err := tx.Model(&account).Updates(map[string]any{
		"mailbox_id": mailbox.ID, "group_name": groupName,
		"account_type": mailbox.AccountType, "access_token": accessToken,
		"trial_eligibility": mailbox.TrialEligibility, "trial_check_error": mailbox.TrialCheckError, "trial_checked_at": mailbox.TrialCheckedAt,
	}).Error; err != nil {
		return err
	}

	var session SunnySession
	if err := tx.Where("account_id = ?", account.ID).First(&session).Error; err != nil {
		_ = tx.Where("email IN ?", emails).First(&session).Error
	}
	if session.ID == 0 {
		session = SunnySession{Email: mailbox.Email, AccountID: account.ID, RefreshToken: mailbox.OpenAIRT, RawMailboxLine: mailbox.Raw,
			AccessTokenStatus: "unknown", HealthCheckStatus: "unknown"}
		if err := tx.Create(&session).Error; err != nil {
			return err
		}
	}
	return tx.Model(&session).Updates(map[string]any{
		"account_id": account.ID, "access_token": accessToken, "raw_mailbox_line": mailbox.Raw,
		"access_token_status": "unknown", "access_token_error": "", "access_token_checked_at": nil,
		"expires_at": nil,
	}).Error
}

func (s *Server) sunnyAccountIDsByEmail(emails []string) map[string]uint {
	out := map[string]uint{}
	if len(emails) == 0 {
		return out
	}
	var accounts []SunnyAccount
	s.db.Select("id", "email").Where("email IN ?", emails).Find(&accounts)
	for _, account := range accounts {
		out[sunnyEmailKey(account.Email)] = account.ID
	}
	return out
}

type sunnyMailboxLinkedData struct {
	sessionPlans     map[string]string
	accountExists    map[string]bool
	accessTokens     map[string]string
	accountIDs       map[string]uint
	accountRTs       map[string]string
	trialEligibility map[string]string
}

func (s *Server) sunnyMailboxLinkedDataByEmail(emails []string) sunnyMailboxLinkedData {
	linked := sunnyMailboxLinkedData{
		sessionPlans:     map[string]string{},
		accountExists:    map[string]bool{},
		accessTokens:     map[string]string{},
		accountIDs:       map[string]uint{},
		accountRTs:       map[string]string{},
		trialEligibility: map[string]string{},
	}
	if len(emails) == 0 {
		return linked
	}
	var accounts []SunnyAccount
	s.db.Select("id", "email", "access_token", "openai_rt", "trial_eligibility").Where("email IN ?", emails).Find(&accounts)
	for _, account := range accounts {
		key := sunnyEmailKey(account.Email)
		linked.accountExists[key] = true
		linked.accountIDs[key] = account.ID
		linked.accountRTs[key] = account.OpenAIRT
		if trialEligibility := normalizeSunnyTrialEligibility(account.TrialEligibility); trialEligibility != sunnyTrialUnknown {
			linked.trialEligibility[key] = trialEligibility
		}
		if strings.TrimSpace(account.AccessToken) != "" {
			linked.accessTokens[key] = sunnyPreferredAccessToken(linked.accessTokens[key], account.AccessToken)
		}
	}
	var sessions []SunnySession
	s.db.Select("email", "access_token", "session_json").Where("email IN ?", emails).Find(&sessions)
	for _, session := range sessions {
		key := sunnyEmailKey(session.Email)
		plan := sunnyPlanTypeFromSessionJSON(session.SessionJSON)
		if plan != "" {
			linked.sessionPlans[key] = plan
		} else if linked.sessionPlans[key] == "" {
			linked.sessionPlans[key] = "free"
		}
		if token := sunnyPreferredAccessToken(session.AccessToken, sunnyAccessTokenFromSessionJSON(session.SessionJSON)); token != "" {
			// Session rows are the authoritative latest source when legacy
			// non-JWT tokens have no comparable expiry.
			linked.accessTokens[key] = sunnyPreferredAccessToken(token, linked.accessTokens[key])
		}
	}
	return linked
}

func sunnyPlanTypeForMailbox(m SunnyMailbox, sessionPlans map[string]string, accountExists map[string]bool) string {
	key := sunnyEmailKey(m.Email)
	if m.RebindEmail != "" && !accountExists[key] {
		key = sunnyEmailKey(m.RebindEmail)
	}
	if plan := normalizeSunnyPlanType(m.AccountType); plan != "" && plan != "free" {
		return plan
	}
	if plan := sessionPlans[key]; plan != "" {
		return plan
	}
	if accountExists[key] || strings.TrimSpace(m.OpenAIRT) != "" || sunnyMailboxStatusLooksRegistered(m.Status) {
		return fallback(normalizeSunnyPlanType(m.AccountType), "free")
	}
	return "-"
}

func (s *Server) sunnyGroupMap() map[uint]string {
	var groups []SunnyMailboxGroup
	s.db.Find(&groups)
	out := map[uint]string{}
	for _, g := range groups {
		out[g.ID] = g.Name
	}
	return out
}

func (s *Server) sunnyMailboxes(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 1 && parts[0] == "config" {
		if r.Method == http.MethodGet {
			cfg := s.sunnyGetConfig(sunnyCfgMailbox, defaultMailboxConfig())
			writeJSON(w, 200, cfg)
			return
		}
		if r.Method == http.MethodPut {
			body, _ := parseBody(r)
			s.sunnySaveConfig(sunnyCfgMailbox, mergeConfig(defaultMailboxConfig(), body))
			writeJSON(w, 200, s.sunnyGetConfig(sunnyCfgMailbox, defaultMailboxConfig()))
			return
		}
	}
	if len(parts) == 0 && r.Method == http.MethodGet {
		q := r.URL.Query()
		selectionOnly := strings.EqualFold(strings.TrimSpace(q.Get("selection")), "all")
		summary := boolValue(q.Get("summary"), false)
		page := intValue(q.Get("page"), 1)
		if page < 1 {
			page = 1
		}
		size := intValue(q.Get("page_size"), 10)
		if size < 1 {
			size = 10
		}
		if size > 100 {
			size = 100
		}
		query := s.db.Model(&SunnyMailbox{})
		if gid := intValue(q.Get("group_id"), 0); gid > 0 {
			query = query.Where("group_id = ?", gid)
		}
		if status := q.Get("status"); status != "" {
			query = query.Where("status IN ?", sunnyMailboxStatusFilterValues(status))
		}
		if enabled := strings.TrimSpace(q.Get("enabled")); enabled != "" {
			query = query.Where("enabled = ?", boolValue(enabled, true))
		}
		if kw := strings.TrimSpace(q.Get("q")); kw != "" {
			like := "%" + kw + "%"
			query = query.Where("LOWER(email) LIKE LOWER(?) OR LOWER(rebind_email) LIKE LOWER(?)", like, like)
		}
		planFilter := normalizeSunnyPlanType(q.Get("plan_type"))
		trialFilter := normalizeSunnyTrialFilter(q.Get("trial_eligibility"))
		rebindEmailFilter := normalizeSunnyRebindEmailFilter(q.Get("rebind_email"))
		passwordFilter := normalizeSunnyLoginSecretFilter(q.Get("password"))
		twoFactorFilter := normalizeSunnyLoginSecretFilter(q.Get("totp"))
		if planFilter != "" || trialFilter != "" || rebindEmailFilter != "" || passwordFilter != "" || twoFactorFilter != "" {
			var allRows []SunnyMailbox
			allQuery := query
			if summary {
				allQuery = allQuery.Select("id", "group_id", "email", "rebind_email", "rebind_mailbox_api", "mailbox_type", "mailbox_channel", "access_key", "password", "client_id", "refresh_token", "raw", "openai_rt", "account_type", "status", "enabled", "registered_at", "chat_gpt_password", "totp_secret", "trial_eligibility", "chatgpt_register_traffic_bytes", "proxy_traffic_bytes", "status_changed_at", "created_at", "updated_at")
			}
			allQuery.Order(sunnyMailboxListSortClause(q.Get("sort_by"), q.Get("sort_order"))).Find(&allRows)
			gm := s.sunnyGroupMap()
			emails := []string{}
			for _, m := range allRows {
				emails = append(emails, m.Email)
				if m.RebindEmail != "" {
					emails = append(emails, m.RebindEmail)
				}
			}
			linked := s.sunnyMailboxLinkedDataByEmail(emails)
			filtered := []map[string]any{}
			for _, m := range allRows {
				key := sunnyEmailKey(m.Email)
				if m.RebindEmail != "" && !linked.accountExists[key] {
					key = sunnyEmailKey(m.RebindEmail)
				}
				plan := sunnyPlanTypeForMailbox(m, linked.sessionPlans, linked.accountExists)
				if planFilter != "" && normalizeSunnyPlanType(plan) != planFilter {
					continue
				}
				trialEligibility := linked.trialEligibility[key]
				if trialEligibility == "" {
					trialEligibility = m.TrialEligibility
				}
				if trialFilter != "" && (!sunnyTrialApplies(m.Status, plan) || normalizeSunnyTrialEligibility(trialEligibility) != trialFilter) {
					continue
				}
				if rebindEmailFilter != "" && (rebindEmailFilter == "present") != (strings.TrimSpace(m.RebindEmail) != "") {
					continue
				}
				if passwordFilter != "" && (passwordFilter == "present") != (strings.TrimSpace(m.ChatGPTPassword) != "") {
					continue
				}
				if twoFactorFilter != "" && (twoFactorFilter == "present") != (strings.TrimSpace(m.TOTPSecret) != "") {
					continue
				}
				item := serializeSunnyMailboxList(m, gm, plan, sunnyMailboxAccessTokenFromLinked(m, linked), linked.accountIDs[key], linked.trialEligibility[key], summary)
				if summary && strings.TrimSpace(linked.accountRTs[key]) != "" {
					item["has_openai_rt"] = true
				}
				filtered = append(filtered, item)
			}
			total := int64(len(filtered))
			if selectionOnly {
				ids := make([]uint, 0, len(filtered))
				selectionItems := make([]map[string]any, 0, len(filtered))
				for _, item := range filtered {
					id := uint(intValue(item["id"], 0))
					if id == 0 {
						continue
					}
					ids = append(ids, id)
					selectionItems = append(selectionItems, map[string]any{"id": id, "email": text(item["email"])})
				}
				writeJSON(w, 200, map[string]any{"ids": ids, "items": selectionItems, "total": len(ids)})
				return
			}
			start := (page - 1) * size
			if start > len(filtered) {
				start = len(filtered)
			}
			end := start + size
			if end > len(filtered) {
				end = len(filtered)
			}
			writeJSON(w, 200, s.sunnyMailboxListResponse(filtered[start:end], total, page, size, summary))
			return
		}
		if selectionOnly {
			var rows []struct {
				ID    uint
				Email string
			}
			query.Select("id", "email").Order("id desc").Scan(&rows)
			ids := make([]uint, 0, len(rows))
			selectionItems := make([]map[string]any, 0, len(rows))
			for _, row := range rows {
				ids = append(ids, row.ID)
				selectionItems = append(selectionItems, map[string]any{"id": row.ID, "email": row.Email})
			}
			writeJSON(w, 200, map[string]any{"ids": ids, "items": selectionItems, "total": len(ids)})
			return
		}
		var total int64
		query.Count(&total)
		var rows []SunnyMailbox
		listQuery := query
		if summary {
			listQuery = listQuery.Select("id", "group_id", "email", "rebind_email", "rebind_mailbox_api", "mailbox_type", "mailbox_channel", "access_key", "password", "client_id", "refresh_token", "raw", "openai_rt", "account_type", "status", "enabled", "registered_at", "chat_gpt_password", "totp_secret", "trial_eligibility", "chatgpt_register_traffic_bytes", "proxy_traffic_bytes", "status_changed_at", "created_at", "updated_at")
		}
		listQuery.Order(sunnyMailboxListSortClause(q.Get("sort_by"), q.Get("sort_order"))).Offset((page - 1) * size).Limit(size).Find(&rows)
		gm := s.sunnyGroupMap()
		emails := []string{}
		for _, m := range rows {
			emails = append(emails, m.Email)
			if m.RebindEmail != "" {
				emails = append(emails, m.RebindEmail)
			}
		}
		linked := s.sunnyMailboxLinkedDataByEmail(emails)
		items := []map[string]any{}
		for _, m := range rows {
			key := sunnyEmailKey(m.Email)
			if m.RebindEmail != "" && !linked.accountExists[key] {
				key = sunnyEmailKey(m.RebindEmail)
			}
			item := serializeSunnyMailboxList(m, gm, sunnyPlanTypeForMailbox(m, linked.sessionPlans, linked.accountExists), sunnyMailboxAccessTokenFromLinked(m, linked), linked.accountIDs[key], linked.trialEligibility[key], summary)
			if summary && strings.TrimSpace(linked.accountRTs[key]) != "" {
				item["has_openai_rt"] = true
			}
			items = append(items, item)
		}
		writeJSON(w, 200, s.sunnyMailboxListResponse(items, total, page, size, summary))
		return
	}
	if len(parts) == 0 && r.Method == http.MethodPost {
		body, _ := parseBody(r)
		m, err := s.sunnyMailboxFromBody(body)
		if err != nil {
			writeError(w, 400, err.Error())
			return
		}
		if err := s.db.Create(&m).Error; err != nil {
			writeError(w, 400, err.Error())
			return
		}
		writeJSON(w, 200, serializeSunnyMailbox(m, s.sunnyGroupMap(), sunnyPlanTypeForMailbox(m, s.sunnySessionPlanTypesByEmail([]string{m.Email}), s.sunnyAccountPresenceByEmail([]string{m.Email})), s.sunnyMailboxAccessToken(m)))
		return
	}
	if len(parts) == 1 && parts[0] == "import" && r.Method == http.MethodPost {
		s.sunnyImportMailboxes(w, r)
		return
	}
	if len(parts) >= 1 {
		id := uint(intValue(parts[0], 0))
		var m SunnyMailbox
		if id == 0 || s.db.First(&m, id).Error != nil {
			writeError(w, 404, "mailbox not found")
			return
		}
		if len(parts) == 1 && r.Method == http.MethodGet {
			key := sunnyEmailKey(m.Email)
			emails := []string{m.Email}
			if m.RebindEmail != "" {
				emails = append(emails, m.RebindEmail)
			}
			linked := s.sunnyMailboxLinkedDataByEmail(emails)
			if m.RebindEmail != "" && !linked.accountExists[key] {
				key = sunnyEmailKey(m.RebindEmail)
			}
			writeJSON(w, 200, serializeSunnyMailboxList(m, s.sunnyGroupMap(), sunnyPlanTypeForMailbox(m, linked.sessionPlans, linked.accountExists), sunnyMailboxAccessTokenFromLinked(m, linked), linked.accountIDs[key], linked.trialEligibility[key], false))
			return
		}
		if len(parts) == 1 && r.Method == http.MethodPut {
			body, _ := parseBody(r)
			originalEmail := m.Email
			requestedEmail := ""
			emailProvided := false
			trialUpdated := false
			mailboxType := normalizeSunnyMailboxType(fallback(text(body["mailbox_type"]), m.MailboxType))
			mailboxChannel := normalizeSunnyMailboxChannel(mailboxType, fallback(text(body["mailbox_channel"]), m.MailboxChannel))
			m.MailboxType, m.MailboxChannel = mailboxType, mailboxChannel
			if _, ok := body["email"]; ok {
				var err error
				requestedEmail, err = normalizeSunnyEditableEmail(text(body["email"]))
				if err != nil {
					writeError(w, http.StatusUnprocessableEntity, err.Error())
					return
				}
				m.Email = requestedEmail
				emailProvided = true
			}
			if _, ok := body["rebind_email"]; ok {
				value := strings.TrimSpace(text(body["rebind_email"]))
				if value != "" {
					normalized, err := normalizeSunnyEditableEmail(value)
					if err != nil {
						writeError(w, http.StatusUnprocessableEntity, err.Error())
						return
					}
					value = normalized
				}
				m.RebindEmail = value
			}
			if _, ok := body["rebind_mailbox_api"]; ok {
				m.RebindMailboxAPI = strings.TrimSpace(text(body["rebind_mailbox_api"]))
			}
			if (m.RebindEmail == "") != (m.RebindMailboxAPI == "") {
				writeError(w, http.StatusUnprocessableEntity, "换绑邮箱名和换绑邮箱 API 必须同时填写")
				return
			}
			if m.RebindEmail != "" {
				if err := validateDomainMailboxAccessKey(m.RebindMailboxAPI, m.RebindEmail); err != nil {
					writeError(w, http.StatusUnprocessableEntity, err.Error())
					return
				}
				mailboxType, mailboxChannel = "domain", "domain_api"
				m.MailboxType, m.MailboxChannel = mailboxType, mailboxChannel
				m.AccessKey = m.RebindMailboxAPI
			}
			if _, ok := body["access_key"]; ok {
				m.AccessKey = text(body["access_key"])
			}
			if m.RebindEmail != "" {
				m.AccessKey = m.RebindMailboxAPI
			}
			if _, ok := body["chatgpt_password"]; ok {
				if value := text(body["chatgpt_password"]); value != "" {
					m.ChatGPTPassword = value
				}
			}
			if boolValue(body["clear_chatgpt_password"], false) {
				m.ChatGPTPassword = ""
			}
			if _, ok := body["totp_secret"]; ok {
				value := text(body["totp_secret"])
				if value == "" {
					// Empty input keeps the current secret; clearing requires clear_totp_secret.
				} else if normalized, err := normalizeSunnyTOTPSecret(value); err != nil {
					writeError(w, http.StatusUnprocessableEntity, err.Error())
					return
				} else {
					m.TOTPSecret = normalized
				}
			}
			if boolValue(body["clear_totp_secret"], false) {
				m.TOTPSecret = ""
			}
			if v := text(body["password"]); v != "" {
				m.Password = v
			}
			if v := text(body["client_id"]); v != "" {
				m.ClientID = v
			}
			if v := text(body["refresh_token"]); v != "" {
				m.RefreshToken = v
			}
			if v := text(body["openai_rt"]); v != "" {
				m.OpenAIRT = v
			}
			if v := text(body["raw"]); v != "" {
				if p, err := parseSunnyMailboxLineForProvider(v, mailboxType, mailboxChannel); err == nil {
					if !emailProvided {
						m.Email = p["email"]
					}
					m.Password = p["password"]
					m.ClientID = p["client_id"]
					m.RefreshToken = p["refresh_token"]
					m.AccessKey = p["access_key"]
					m.ChatGPTPassword = p["chatgpt_password"]
					m.TOTPSecret = p["totp_secret"]
					m.Raw = v
					if p["openai_rt"] != "" {
						m.OpenAIRT = p["openai_rt"]
					}
				}
			}
			normalizedEmail, err := normalizeSunnyEditableEmail(m.Email)
			if err != nil {
				writeError(w, http.StatusUnprocessableEntity, err.Error())
				return
			}
			m.Email = normalizedEmail
			if sunnyEmailKey(originalEmail) != sunnyEmailKey(m.Email) {
				if err := sunnyEmailRenameConflict(s.db, m.Email); err != nil {
					if strings.Contains(err.Error(), "已被其他") {
						writeError(w, http.StatusConflict, err.Error())
					} else {
						writeError(w, http.StatusInternalServerError, err.Error())
					}
					return
				}
			}
			if mailboxType == "remail" {
				if strings.TrimSpace(m.AccessKey) == "" {
					writeError(w, http.StatusUnprocessableEntity, sunnyMailboxFormatHint(mailboxType, mailboxChannel))
					return
				}
				m.Password, m.ClientID, m.RefreshToken = "", "", ""
				m.Raw = strings.Join([]string{strings.TrimSpace(m.Email), strings.TrimSpace(m.AccessKey)}, "----")
			} else if mailboxType == "domain" {
				if mailboxChannel != "domain_api" || strings.TrimSpace(m.AccessKey) == "" {
					writeError(w, http.StatusUnprocessableEntity, sunnyMailboxFormatHint(mailboxType, mailboxChannel))
					return
				}
				credentialEmail := m.Email
				if m.RebindEmail != "" {
					credentialEmail = m.RebindEmail
				}
				if m.RebindMailboxAPI != "" {
					m.AccessKey = m.RebindMailboxAPI
				}
				if err := validateDomainMailboxAccessKey(m.AccessKey, credentialEmail); err != nil {
					writeError(w, http.StatusUnprocessableEntity, sunnyMailboxFormatHint(mailboxType, mailboxChannel))
					return
				}
				m.PickupTokenHash = domainMailboxTokenHashFromCredential(m.AccessKey, credentialEmail)
				m.Password, m.ClientID, m.RefreshToken = "", "", ""
				m.Raw = strings.Join([]string{strings.TrimSpace(credentialEmail), strings.TrimSpace(m.AccessKey)}, "----")
			} else if mailboxType == "apple" {
				if mailboxChannel != "xbovo" && mailboxChannel != "url_api" {
					writeError(w, http.StatusUnprocessableEntity, "暂不支持该 iCloud 邮箱渠道")
					return
				}
				if mailboxChannel == "url_api" && m.AccessKey != "" {
					if _, err := validateURLAPIMailAddress(m.AccessKey); err != nil {
						writeError(w, http.StatusUnprocessableEntity, sunnyMailboxFormatHint(mailboxType, mailboxChannel))
						return
					}
				}
				m.Password, m.ClientID, m.RefreshToken = "", "", ""
				if mailboxChannel == "url_api" {
					m.Raw = sunnyURLAPIRaw(m.Email, m.AccessKey)
				} else {
					m.Raw = strings.Join([]string{strings.TrimSpace(m.Email), strings.TrimSpace(m.AccessKey)}, "----")
				}
			} else {
				m.AccessKey = ""
				m.Raw = sunnyMicrosoftRaw(m.Email, m.Password, m.ClientID, m.RefreshToken)
			}
			if gid := uint(intValue(body["group_id"], 0)); gid > 0 {
				m.GroupID = gid
			}
			if v := normalizeSunnyMailboxStatus(text(body["status"])); v != "" {
				m.Status = v
			}
			if v := fallback(text(body["plan_type"]), text(body["account_type"])); v != "" && v != "-" {
				m.AccountType = normalizeSunnyPlanType(v)
			}
			if _, ok := body["enabled"]; ok {
				m.Enabled = boolValue(body["enabled"], m.Enabled)
			}
			if _, ok := body["last_error"]; ok {
				m.LastError = text(body["last_error"])
			}
			if _, ok := body["trial_eligibility"]; ok {
				trialUpdated = true
				m.TrialEligibility = normalizeSunnyTrialEligibility(text(body["trial_eligibility"]))
				m.TrialCheckError = ""
				m.TrialCheckedAt = sunnyManualTrialCheckedAt(m.TrialEligibility)
			}
			groupName := s.sunnyGroupMap()[m.GroupID]
			if err := s.db.Transaction(func(tx *gorm.DB) error {
				if err := tx.Save(&m).Error; err != nil {
					return err
				}
				if originalEmail != m.Email {
					if err := tx.Model(&SunnyAccount{}).Where("LOWER(email) = ?", sunnyEmailKey(originalEmail)).Update("email", m.Email).Error; err != nil {
						return err
					}
					if err := tx.Model(&SunnySession{}).Where("LOWER(email) = ?", sunnyEmailKey(originalEmail)).Updates(map[string]any{
						"email": m.Email, "raw_mailbox_line": sunnyMailboxRawForEmail(m, m.Email),
					}).Error; err != nil {
						return err
					}
				}
				if m.RebindEmail != "" {
					if err := tx.Model(&SunnyAccount{}).Where("mailbox_id = ?", m.ID).Updates(map[string]any{
						"rebind_email": m.RebindEmail, "rebind_mailbox_api": m.RebindMailboxAPI,
					}).Error; err != nil {
						return err
					}
					var linkedAccounts []SunnyAccount
					if err := tx.Select("id").Where("mailbox_id = ?", m.ID).Find(&linkedAccounts).Error; err != nil {
						return err
					}
					accountIDs := make([]uint, 0, len(linkedAccounts))
					for _, account := range linkedAccounts {
						accountIDs = append(accountIDs, account.ID)
					}
					if len(accountIDs) > 0 {
						if err := tx.Model(&SunnySession{}).Where("account_id IN ?", accountIDs).Update("raw_mailbox_line", sunnyMailboxCredentialLine(m)).Error; err != nil {
							return err
						}
					}
				}
				if v, ok := body["access_token"]; ok {
					if err := sunnySaveMailboxAccessToken(tx, m, groupName, text(v)); err != nil {
						return err
					}
				}
				if m.AccountType != "" {
					if err := tx.Model(&SunnyAccount{}).Where("email = ?", m.Email).Update("account_type", m.AccountType).Error; err != nil {
						return err
					}
				}
				if trialUpdated {
					if err := tx.Model(&SunnyAccount{}).Where("email = ?", m.Email).Updates(map[string]any{
						"trial_eligibility": m.TrialEligibility, "trial_check_error": "", "trial_checked_at": m.TrialCheckedAt,
					}).Error; err != nil {
						return err
					}
				}
				return nil
			}); err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
			writeJSON(w, 200, serializeSunnyMailbox(m, s.sunnyGroupMap(), sunnyPlanTypeForMailbox(m, s.sunnySessionPlanTypesByEmail([]string{m.Email}), s.sunnyAccountPresenceByEmail([]string{m.Email})), s.sunnyMailboxAccessToken(m)))
			return
		}
		if len(parts) == 1 && r.Method == http.MethodDelete {
			s.db.Delete(&m)
			writeJSON(w, 200, map[string]any{"ok": true})
			return
		}
		if len(parts) == 2 && parts[1] == "latest-mail" && r.Method == http.MethodPost {
			s.sunnyLatestMail(w, r, &m)
			return
		}
		if len(parts) == 2 && parts[1] == "url-api-preview" && r.Method == http.MethodGet {
			s.sunnyURLAPIPreview(w, r, &m)
			return
		}
		if len(parts) == 2 && parts[1] == "field" && r.Method == http.MethodGet {
			field := strings.TrimSpace(r.URL.Query().Get("name"))
			value := ""
			switch field {
			case "access_token":
				value = s.sunnyMailboxAccessToken(m)
			case "secret_key":
				value = sunnyMailboxCredentialLine(m)
			case "chatgpt_password":
				value = m.ChatGPTPassword
			case "totp_secret":
				value = m.TOTPSecret
			case "login_secret":
				value = sunnyLoginSecretLine(m)
			default:
				writeError(w, 400, "unsupported mailbox field")
				return
			}
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("Pragma", "no-cache")
			writeJSON(w, 200, map[string]any{"field": field, "value": value})
			return
		}
	}
	writeError(w, 404, "not found")
}

func (s *Server) sunnyMailboxFromBody(body map[string]any) (SunnyMailbox, error) {
	mailboxType := normalizeSunnyMailboxType(text(body["mailbox_type"]))
	mailboxChannel := normalizeSunnyMailboxChannel(mailboxType, text(body["mailbox_channel"]))
	raw := text(body["raw"])
	email, password, chatgptPassword, totpSecret, clientID, refreshToken, accessKey, openaiRT := "", "", "", "", "", "", "", ""
	if raw != "" {
		p, err := parseSunnyMailboxLineForProvider(raw, mailboxType, mailboxChannel)
		if err != nil {
			return SunnyMailbox{}, err
		}
		email, password, chatgptPassword, totpSecret, clientID, refreshToken, accessKey, openaiRT = p["email"], p["password"], p["chatgpt_password"], p["totp_secret"], p["client_id"], p["refresh_token"], p["access_key"], p["openai_rt"]
		if mailboxType == "remail" || mailboxType == "domain" {
			raw = strings.Join([]string{email, accessKey}, "----")
		} else if mailboxType == "microsoft" {
			raw = sunnyMicrosoftRaw(email, password, clientID, refreshToken)
		} else if mailboxChannel == "url_api" {
			raw = sunnyURLAPIRaw(email, accessKey)
		} else {
			raw = strings.Join([]string{email, accessKey}, "----")
		}
	} else {
		email, password, chatgptPassword, totpSecret, clientID, refreshToken, accessKey, openaiRT = text(body["email"]), text(body["password"]), text(body["chatgpt_password"]), text(body["totp_secret"]), text(body["client_id"]), text(body["refresh_token"]), text(body["access_key"]), text(body["openai_rt"])
		if mailboxType == "remail" || mailboxType == "domain" {
			raw = strings.Join([]string{email, accessKey}, "----")
		} else if mailboxType == "apple" {
			if mailboxChannel == "url_api" {
				raw = sunnyURLAPIRaw(email, accessKey)
			} else {
				raw = strings.Join([]string{email, accessKey}, "----")
			}
		} else {
			raw = sunnyMicrosoftRaw(email, password, clientID, refreshToken)
		}
	}
	if email == "" || !strings.Contains(email, "@") || (mailboxType == "apple" && mailboxChannel != "url_api" && accessKey == "") || (mailboxType == "microsoft" && (clientID == "" || refreshToken == "")) || ((mailboxType == "remail" || mailboxType == "domain") && accessKey == "") {
		return SunnyMailbox{}, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, mailboxChannel))
	}
	if mailboxType == "apple" {
		if mailboxChannel != "xbovo" && mailboxChannel != "url_api" {
			return SunnyMailbox{}, fmt.Errorf("暂不支持该 iCloud 邮箱渠道")
		}
		if mailboxChannel == "url_api" && accessKey != "" {
			if _, err := validateURLAPIMailAddress(accessKey); err != nil {
				return SunnyMailbox{}, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, mailboxChannel))
			}
		}
	}
	if mailboxType == "domain" {
		if mailboxChannel != "domain_api" {
			return SunnyMailbox{}, fmt.Errorf("自建域名邮箱渠道必须为 domain_api")
		}
		if err := validateDomainMailboxAccessKey(accessKey, email); err != nil {
			return SunnyMailbox{}, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, mailboxChannel))
		}
	}
	gid := uint(intValue(body["group_id"], 0))
	if gid == 0 {
		gid = s.sunnyEnsureDefaultGroup()
	}
	enabled := boolValue(body["enabled"], true)
	status := fallback(normalizeSunnyMailboxStatus(text(body["status"])), "未注册")
	if openaiRT != "" && status == "未注册" {
		status = "已注册"
	}
	return SunnyMailbox{GroupID: gid, Email: email, MailboxType: mailboxType, MailboxChannel: mailboxChannel, AccessKey: accessKey, PickupTokenHash: domainMailboxTokenHashFromCredential(accessKey, email), Password: password, ChatGPTPassword: chatgptPassword, TOTPSecret: totpSecret, ClientID: clientID, RefreshToken: refreshToken, OpenAIRT: openaiRT, Raw: raw, AccountType: fallback(normalizeSunnyPlanType(fallback(text(body["plan_type"]), text(body["account_type"]))), "free"), Status: status, Enabled: enabled, LatestMailJSON: "{}"}, nil
}

func normalizeSunnyMailboxType(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "remail", "remail邮箱", "rm":
		return "remail"
	case "domain", "domain邮箱", "自建域名邮箱", "cloudmail", "cfworker":
		return "domain"
	case "apple", "icloud":
		return "apple"
	default:
		return "microsoft"
	}
}

func normalizeSunnyMailboxChannel(mailboxType, value string) string {
	if normalizeSunnyMailboxType(mailboxType) == "remail" {
		return "remail_api"
	}
	if normalizeSunnyMailboxType(mailboxType) == "apple" {
		normalized := strings.ToLower(strings.TrimSpace(value))
		if normalized == "xbovo" || normalized == "" {
			return "xbovo"
		}
		if normalized == "url_api" || normalized == "url-api" {
			return "url_api"
		}
		return normalized
	}
	if normalizeSunnyMailboxType(mailboxType) == "domain" {
		return "domain_api"
	}
	return "outlook"
}

func sunnyMailboxFormatHint(mailboxType, channel string) string {
	if normalizeSunnyMailboxType(mailboxType) == "remail" {
		return "Remail 邮箱凭证格式必须为 email----serviceToken 或 email----Remail 凭证 JSON"
	}
	if normalizeSunnyMailboxType(mailboxType) == "apple" {
		if normalizeSunnyMailboxChannel(mailboxType, channel) == "url_api" {
			return "url_api 苹果邮箱凭证支持 邮箱、邮箱----密码、邮箱----取码URL，以及可选的备用取码URL和2FA密钥"
		}
		return "苹果邮箱凭证格式必须为 icloud_email----key"
	}
	if normalizeSunnyMailboxType(mailboxType) == "domain" {
		return "自建域名邮箱凭证格式必须为 email----独立取件URL（旧版 API 凭证 JSON 仍兼容）"
	}
	return "微软邮箱凭证格式必须为 email----password----client_id----refresh_token"
}

func normalizeSunnyMailboxStatus(status string) string {
	status = strings.TrimSpace(status)
	if status == "PLUS试用中" {
		return "已接码"
	}
	return status
}

type sunnyMailboxStatusCountRow struct {
	Status string `gorm:"column:status"`
	Count  int64  `gorm:"column:count"`
}

func normalizeSunnyMailboxCountStatus(status string) string {
	raw := normalizeSunnyMailboxStatus(status)
	switch strings.ToLower(raw) {
	case "", "pending", "unregistered":
		return "未注册"
	case "registered", "success", "succeeded":
		return "已注册"
	case "phone_bound", "phone-bound", "bound":
		return "已接码"
	case "reverse_proxied", "reverse-proxied", "proxied", "imported":
		return "已反代"
	case "banned", "disabled":
		return "已封禁"
	case "needs_2fa", "needs-2fa", "2fa":
		return "需二验"
	case "failed", "error":
		return "失败"
	default:
		return raw
	}
}

func sunnyMailboxStatusFilterValues(status string) []string {
	switch normalizeSunnyMailboxCountStatus(status) {
	case "未注册":
		return []string{"未注册", "pending", "unregistered"}
	case "已注册":
		return []string{"已注册", "registered", "success", "succeeded"}
	case "已接码":
		return []string{"已接码", "phone_bound", "phone-bound", "bound", "PLUS试用中"}
	case "已反代":
		return []string{"已反代", "reverse_proxied", "reverse-proxied", "proxied", "imported"}
	case "已封禁":
		return []string{"已封禁", "banned", "disabled"}
	case "需二验":
		return []string{"需二验", "needs_2fa", "needs-2fa", "2fa"}
	case "失败":
		return []string{"失败", "failed", "error"}
	default:
		return []string{strings.TrimSpace(status)}
	}
}

func (s *Server) sunnyMailboxStatusCounts() (map[string]int64, int64) {
	counts := make(map[string]int64, len(sunnyMailboxStatuses))
	for _, status := range sunnyMailboxStatuses {
		counts[status] = 0
	}
	var rows []sunnyMailboxStatusCountRow
	s.db.Model(&SunnyMailbox{}).Select("status, COUNT(*) AS count").Group("status").Scan(&rows)
	var total int64
	for _, row := range rows {
		total += row.Count
		status := normalizeSunnyMailboxCountStatus(row.Status)
		if _, ok := counts[status]; ok {
			counts[status] += row.Count
		}
	}
	return counts, total
}

func (s *Server) sunnyMailboxListResponse(items []map[string]any, total int64, page, pageSize int, summary bool) map[string]any {
	response := map[string]any{"items": items, "total": total, "page": page, "page_size": pageSize, "statuses": sunnyMailboxStatuses}
	if summary {
		counts, mailboxTotal := s.sunnyMailboxStatusCounts()
		response["status_counts"] = counts
		response["mailbox_total"] = mailboxTotal
	}
	return response
}

func parseSunnyMailboxLine(raw string) (map[string]string, error) {
	parts := strings.Split(strings.TrimSpace(raw), "----")
	if len(parts) < 4 {
		return nil, fmt.Errorf("格式错误，应为 email----password----client_id----refresh_token")
	}
	for index := range parts {
		parts[index] = strings.TrimSpace(parts[index])
	}
	email := parts[0]
	password, clientID, rt := normalizeSunnyMicrosoftCredentials(parts[1], parts[2], parts[3])
	if email == "" || !strings.Contains(email, "@") || clientID == "" || rt == "" {
		return nil, fmt.Errorf("email / client_id / refresh_token 不能为空")
	}
	out := map[string]string{"email": email, "password": password, "chatgpt_password": "", "totp_secret": "", "client_id": clientID, "refresh_token": rt, "access_key": "", "openai_rt": ""}
	for _, extra := range parts[4:] {
		extra = strings.TrimSpace(extra)
		lower := strings.ToLower(extra)
		if strings.HasPrefix(lower, "rt_token=") || strings.HasPrefix(lower, "openai_rt=") {
			_, v, _ := strings.Cut(extra, "=")
			out["openai_rt"] = strings.TrimSpace(v)
		}
	}
	return out, nil
}

var sunnyMicrosoftClientIDPattern = regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

func isSunnyMicrosoftClientID(value string) bool {
	return sunnyMicrosoftClientIDPattern.MatchString(strings.TrimSpace(value))
}

func isSunnyMicrosoftRefreshToken(value string) bool {
	value = strings.TrimSpace(value)
	lower := strings.ToLower(value)
	if strings.HasPrefix(lower, "m.c") || strings.HasPrefix(lower, "m.r") || strings.HasPrefix(lower, "0.a") || strings.HasPrefix(lower, "1.a") {
		return true
	}
	return len(value) >= 80 && (strings.ContainsAny(value, "!*$") || strings.Count(value, ".") >= 2)
}

func normalizeSunnyMicrosoftCredentials(first, second, third string) (password, clientID, refreshToken string) {
	values := []string{strings.TrimSpace(first), strings.TrimSpace(second), strings.TrimSpace(third)}
	clientIndex, refreshIndex := -1, -1
	for index, value := range values {
		if clientIndex < 0 && isSunnyMicrosoftClientID(value) {
			clientIndex = index
		}
		if refreshIndex < 0 && isSunnyMicrosoftRefreshToken(value) {
			refreshIndex = index
		}
	}
	if clientIndex >= 0 && refreshIndex >= 0 && clientIndex != refreshIndex {
		for index, value := range values {
			switch index {
			case clientIndex:
				clientID = value
			case refreshIndex:
				refreshToken = value
			default:
				password = value
			}
		}
		return password, clientID, refreshToken
	}
	return values[0], values[1], values[2]
}

func sunnyMicrosoftRaw(email, password, clientID, refreshToken string) string {
	return strings.Join([]string{
		strings.TrimSpace(email), strings.TrimSpace(password), strings.TrimSpace(clientID), strings.TrimSpace(refreshToken),
	}, "----")
}

func normalizeSunnyTOTPSecret(value string) (string, error) {
	normalized := strings.ToUpper(strings.TrimSpace(value))
	normalized = strings.NewReplacer(" ", "", "\t", "", "\r", "", "\n", "", "=", "").Replace(normalized)
	if !regexp.MustCompile(`^[A-Z2-7]{16,128}$`).MatchString(normalized) {
		return "", fmt.Errorf("2FA 密钥格式错误，只能包含 Base32 字符 A-Z 和 2-7")
	}
	return normalized, nil
}

func isSunnyHTTPURL(value string) bool {
	lower := strings.ToLower(strings.TrimSpace(value))
	return strings.HasPrefix(lower, "http://") || strings.HasPrefix(lower, "https://")
}

func parseSunnyURLAPIMailboxLine(raw string) (map[string]string, error) {
	parts := strings.Split(strings.TrimSpace(raw), "----")
	if len(parts) < 1 || len(parts) > 4 {
		return nil, fmt.Errorf("url_api 邮箱凭证格式错误")
	}
	for index := range parts {
		parts[index] = strings.TrimSpace(parts[index])
	}
	email := parts[0]
	if email == "" || !strings.Contains(email, "@") {
		return nil, fmt.Errorf("url_api 邮箱地址格式错误")
	}
	out := map[string]string{"email": email, "password": "", "chatgpt_password": "", "totp_secret": "", "client_id": "", "refresh_token": "", "access_key": "", "openai_rt": ""}
	remaining := parts[1:]
	if len(remaining) > 0 && remaining[0] != "" {
		if isSunnyHTTPURL(remaining[0]) {
			out["access_key"] = remaining[0]
		} else {
			out["chatgpt_password"] = remaining[0]
		}
	}
	if len(remaining) > 1 {
		for _, value := range remaining[1:] {
			if value == "" {
				return nil, fmt.Errorf("url_api 邮箱凭证包含空字段")
			}
			if isSunnyHTTPURL(value) {
				if out["access_key"] != "" {
					return nil, fmt.Errorf("url_api 邮箱凭证只能包含一个收码 URL")
				}
				out["access_key"] = value
				continue
			}
			if out["totp_secret"] != "" {
				return nil, fmt.Errorf("url_api 邮箱凭证包含无法识别的字段")
			}
			secret, err := normalizeSunnyTOTPSecret(value)
			if err != nil {
				return nil, err
			}
			out["totp_secret"] = secret
		}
	}
	if out["access_key"] != "" {
		if _, err := validateURLAPIMailAddress(out["access_key"]); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func sunnyURLAPIRaw(email, accessKey string) string {
	parts := []string{strings.TrimSpace(email)}
	if value := strings.TrimSpace(accessKey); value != "" {
		parts = append(parts, value)
	}
	return strings.Join(parts, "----")
}

func sunnyMailboxCredentialLine(mailbox SunnyMailbox) string {
	if rebindEmail := strings.TrimSpace(mailbox.RebindEmail); rebindEmail != "" {
		accessKey := strings.TrimSpace(mailbox.RebindMailboxAPI)
		if accessKey != "" {
			return strings.Join([]string{rebindEmail, accessKey}, "----")
		}
		return ""
	}
	mailboxType := normalizeSunnyMailboxType(mailbox.MailboxType)
	mailboxChannel := normalizeSunnyMailboxChannel(mailbox.MailboxType, mailbox.MailboxChannel)
	if mailboxType == "remail" {
		if strings.TrimSpace(mailbox.Email) != "" && strings.TrimSpace(mailbox.AccessKey) != "" {
			return strings.Join([]string{strings.TrimSpace(mailbox.Email), strings.TrimSpace(mailbox.AccessKey)}, "----")
		}
		return sunnyCanonicalMailboxCredential(mailbox.Raw, mailboxType, mailboxChannel)
	}
	if mailboxType == "domain" {
		if strings.TrimSpace(mailbox.Email) != "" && strings.TrimSpace(mailbox.AccessKey) != "" {
			return strings.Join([]string{strings.TrimSpace(mailbox.Email), strings.TrimSpace(mailbox.AccessKey)}, "----")
		}
		return sunnyCanonicalMailboxCredential(mailbox.Raw, mailboxType, mailboxChannel)
	}
	if mailboxType == "apple" {
		if mailboxChannel == "url_api" {
			if strings.TrimSpace(mailbox.Email) == "" || strings.TrimSpace(mailbox.AccessKey) == "" {
				return sunnyCanonicalMailboxCredential(mailbox.Raw, mailboxType, mailboxChannel)
			}
			return sunnyURLAPIRaw(mailbox.Email, mailbox.AccessKey)
		}
		if strings.TrimSpace(mailbox.Email) != "" && strings.TrimSpace(mailbox.AccessKey) != "" {
			return strings.Join([]string{strings.TrimSpace(mailbox.Email), strings.TrimSpace(mailbox.AccessKey)}, "----")
		}
	} else if strings.TrimSpace(mailbox.Email) != "" && strings.TrimSpace(mailbox.Password) != "" && strings.TrimSpace(mailbox.ClientID) != "" && strings.TrimSpace(mailbox.RefreshToken) != "" {
		return sunnyMicrosoftRaw(mailbox.Email, mailbox.Password, mailbox.ClientID, mailbox.RefreshToken)
	}
	return sunnyCanonicalMailboxCredential(mailbox.Raw, mailboxType, mailboxChannel)
}

func sunnyCanonicalMailboxCredential(raw, mailboxType, mailboxChannel string) string {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return ""
	}
	if strings.TrimSpace(mailboxType) == "" && (strings.Contains(raw, "----http://") || strings.Contains(raw, "----https://")) {
		mailboxType, mailboxChannel = "apple", "url_api"
	}
	p, err := parseSunnyMailboxLineForProvider(raw, mailboxType, mailboxChannel)
	if err != nil {
		return raw
	}
	return sunnyMailboxCredentialLine(SunnyMailbox{
		Email:          p["email"],
		MailboxType:    mailboxType,
		MailboxChannel: mailboxChannel,
		Password:       p["password"],
		ClientID:       p["client_id"],
		RefreshToken:   p["refresh_token"],
		AccessKey:      p["access_key"],
	})
}

func sunnyCredentialPreview(value string) string {
	runes := []rune(strings.TrimSpace(value))
	if len(runes) == 0 {
		return ""
	}
	if len(runes) > 4 {
		runes = runes[:4]
	}
	return string(runes) + "••••••"
}

func sunnyLoginSecretLine(mailbox SunnyMailbox) string {
	email := strings.TrimSpace(mailbox.Email)
	if strings.TrimSpace(mailbox.RebindEmail) != "" {
		email = strings.TrimSpace(mailbox.RebindEmail)
	}
	password := strings.TrimSpace(mailbox.ChatGPTPassword)
	totp := strings.TrimSpace(mailbox.TOTPSecret)
	if email == "" || password == "" || totp == "" {
		return ""
	}
	return strings.Join([]string{email, password, totp}, "----")
}

func sunnySub2Notes(mailbox SunnyMailbox, secretKey string) string {
	lines := []string{}
	if value := strings.TrimSpace(secretKey); value != "" {
		lines = append(lines, "邮箱凭证："+value)
	}
	if value := sunnyLoginSecretLine(mailbox); value != "" {
		lines = append(lines, "密码2FA："+value)
	}
	return strings.Join(lines, "\n")
}

func sunnySub2NotesWithConfig(mailbox SunnyMailbox, secretKey string, cfg map[string]any) string {
	lines := []string{}
	if boolValue(cfg["notes_include_sk"], false) {
		if value := strings.TrimSpace(secretKey); value != "" {
			lines = append(lines, "邮箱凭证："+value)
		}
	}
	if boolValue(cfg["notes_include_ls"], false) {
		if value := sunnyLoginSecretLine(mailbox); value != "" {
			lines = append(lines, "密码2FA："+value)
		}
	}
	if boolValue(cfg["notes_include_custom"], false) {
		if value := strings.TrimSpace(text(cfg["notes_custom_text"])); value != "" {
			lines = append(lines, value)
		}
	}
	return strings.Join(lines, "\n")
}

func parseSunnyMailboxLineForProvider(raw, mailboxType, channel string) (map[string]string, error) {
	if normalizeSunnyMailboxType(mailboxType) == "remail" {
		parts := strings.Split(strings.TrimSpace(raw), "----")
		if len(parts) < 2 || strings.TrimSpace(parts[0]) == "" || !strings.Contains(parts[0], "@") || strings.TrimSpace(parts[1]) == "" {
			return nil, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, channel))
		}
		return map[string]string{"email": strings.TrimSpace(parts[0]), "password": "", "chatgpt_password": "", "totp_secret": "", "client_id": "", "refresh_token": "", "access_key": strings.TrimSpace(strings.Join(parts[1:], "----")), "openai_rt": ""}, nil
	}
	if normalizeSunnyMailboxType(mailboxType) == "domain" {
		parts := strings.SplitN(strings.TrimSpace(raw), "----", 2)
		if len(parts) != 2 || strings.TrimSpace(parts[0]) == "" || !strings.Contains(parts[0], "@") || strings.TrimSpace(parts[1]) == "" {
			return nil, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, channel))
		}
		if err := validateDomainMailboxAccessKey(strings.TrimSpace(parts[1]), strings.TrimSpace(parts[0])); err != nil {
			return nil, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, channel))
		}
		return map[string]string{"email": strings.TrimSpace(parts[0]), "password": "", "chatgpt_password": "", "totp_secret": "", "client_id": "", "refresh_token": "", "access_key": strings.TrimSpace(parts[1]), "openai_rt": ""}, nil
	}
	if normalizeSunnyMailboxType(mailboxType) != "apple" {
		return parseSunnyMailboxLine(raw)
	}
	normalizedChannel := normalizeSunnyMailboxChannel(mailboxType, channel)
	if normalizedChannel != "xbovo" && normalizedChannel != "url_api" {
		return nil, fmt.Errorf("暂不支持该苹果邮箱渠道")
	}
	if normalizedChannel == "url_api" {
		return parseSunnyURLAPIMailboxLine(raw)
	}
	parts := strings.Split(strings.TrimSpace(raw), "----")
	if len(parts) != 2 {
		return nil, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, channel))
	}
	email, accessKey := strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1])
	if email == "" || !strings.Contains(email, "@") || accessKey == "" {
		return nil, fmt.Errorf("%s", sunnyMailboxFormatHint(mailboxType, channel))
	}
	return map[string]string{"email": email, "password": "", "chatgpt_password": "", "totp_secret": "", "client_id": "", "refresh_token": "", "access_key": accessKey, "openai_rt": ""}, nil
}

func (s *Server) sunnyImportMailboxes(w http.ResponseWriter, r *http.Request) {
	body := s.sunnyReadImportBody(r)
	mailboxType := normalizeSunnyMailboxType(text(body["mailbox_type"]))
	mailboxChannel := normalizeSunnyMailboxChannel(mailboxType, text(body["mailbox_channel"]))
	gid := uint(intValue(body["group_id"], 0))
	if gid == 0 && text(body["group_name"]) != "" {
		g := SunnyMailboxGroup{Name: text(body["group_name"])}
		s.db.FirstOrCreate(&g, SunnyMailboxGroup{Name: g.Name})
		gid = g.ID
	}
	if gid == 0 {
		gid = s.sunnyEnsureDefaultGroup()
	}
	lines := strings.Split(text(body["lines"]), "\n")
	ok, bad := 0, []string{}
	parsed := map[string]map[string]string{}
	order := []string{}
	for _, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}
		p, err := parseSunnyMailboxLineForProvider(line, mailboxType, mailboxChannel)
		if err != nil {
			bad = append(bad, line+" => "+err.Error())
			continue
		}
		key := sunnyEmailKey(p["email"])
		if _, exists := parsed[key]; !exists {
			order = append(order, key)
		}
		if mailboxType == "microsoft" {
			p["raw"] = sunnyMicrosoftRaw(p["email"], p["password"], p["client_id"], p["refresh_token"])
		} else {
			p["raw"] = sunnyURLAPIRaw(p["email"], p["access_key"])
		}
		parsed[key] = p
	}
	for _, key := range order {
		p := parsed[key]
		var old SunnyMailbox
		if err := s.db.Where("lower(email) = ?", key).First(&old).Error; err == nil {
			updates := map[string]any{
				"group_id": gid, "mailbox_type": mailboxType, "mailbox_channel": mailboxChannel,
				"access_key": p["access_key"], "password": p["password"], "chat_gpt_password": p["chatgpt_password"],
				"totp_secret": p["totp_secret"], "client_id": p["client_id"], "refresh_token": p["refresh_token"], "raw": p["raw"],
			}
			if mailboxType == "domain" {
				updates["pickup_token_hash"] = domainMailboxTokenHashFromCredential(p["access_key"], p["email"])
			}
			if p["openai_rt"] != "" {
				updates["openai_rt"] = p["openai_rt"]
			}
			if err := s.db.Model(&old).Updates(updates).Error; err != nil {
				bad = append(bad, p["email"]+" => "+err.Error())
				continue
			}
		} else {
			status := "未注册"
			if p["openai_rt"] != "" {
				status = "已注册"
			}
			m := SunnyMailbox{GroupID: gid, Email: p["email"], MailboxType: mailboxType, MailboxChannel: mailboxChannel, AccessKey: p["access_key"], PickupTokenHash: domainMailboxTokenHashFromCredential(p["access_key"], p["email"]), Password: p["password"], ChatGPTPassword: p["chatgpt_password"], TOTPSecret: p["totp_secret"], ClientID: p["client_id"], RefreshToken: p["refresh_token"], OpenAIRT: p["openai_rt"], Raw: p["raw"], AccountType: "free", Status: status, Enabled: true, LatestMailJSON: "{}"}
			if err := s.db.Create(&m).Error; err != nil {
				bad = append(bad, p["email"]+" => "+err.Error())
				continue
			}
		}
		ok++
	}
	writeJSON(w, 200, map[string]any{"ok": true, "imported": ok, "failed": len(bad), "errors": bad})
}

func (s *Server) sunnyReadImportBody(r *http.Request) map[string]any {
	ct := r.Header.Get("Content-Type")
	if strings.Contains(ct, "multipart/form-data") {
		_ = r.ParseMultipartForm(32 << 20)
		out := map[string]any{"lines": r.FormValue("lines"), "group_id": r.FormValue("group_id"), "group_name": r.FormValue("group_name"), "mailbox_type": r.FormValue("mailbox_type"), "mailbox_channel": r.FormValue("mailbox_channel")}
		if r.MultipartForm != nil {
			for _, files := range r.MultipartForm.File {
				for _, fh := range files {
					if f, err := fh.Open(); err == nil {
						b, _ := io.ReadAll(io.LimitReader(f, 16<<20))
						_ = f.Close()
						out["lines"] = text(out["lines"]) + "\n" + string(b)
						break
					}
				}
			}
		}
		return out
	}
	body, _ := parseBody(r)
	return body
}

func (s *Server) sunnyLatestMail(w http.ResponseWriter, r *http.Request, m *SunnyMailbox) {
	body, _ := parseBody(r)
	limit := toInt(body["limit"])
	if limit <= 0 {
		limit = toInt(r.URL.Query().Get("limit"))
	}
	if limit <= 0 {
		limit = 5
	}
	if limit > 50 {
		limit = 50
	}
	proxyURL := s.sunnyMailboxProxyURL()
	mailEmail := strings.TrimSpace(m.Email)
	mailAccessKey := strings.TrimSpace(m.AccessKey)
	rebindEmail := strings.TrimSpace(m.RebindEmail)
	rebindAccessKey := strings.TrimSpace(m.RebindMailboxAPI)
	hasRebindCredential := rebindEmail != "" || rebindAccessKey != ""
	if hasRebindCredential {
		if rebindEmail == "" || rebindAccessKey == "" {
			writeError(w, http.StatusUnprocessableEntity, "换绑邮箱名和换绑邮箱 API 配置不完整")
			return
		}
		mailEmail, mailAccessKey = rebindEmail, rebindAccessKey
	}
	var payload map[string]any
	var err error
	if hasRebindCredential {
		payload, err = s.domainMailLatestMail(mailAccessKey, mailEmail, limit)
	} else if normalizeSunnyMailboxType(m.MailboxType) == "remail" {
		payload, err = remailLatestMail(mailAccessKey, mailEmail, limit)
	} else if normalizeSunnyMailboxType(m.MailboxType) == "domain" {
		payload, err = s.domainMailLatestMail(mailAccessKey, mailEmail, limit)
	} else if normalizeSunnyMailboxType(m.MailboxType) == "apple" {
		switch normalizeSunnyMailboxChannel(m.MailboxType, m.MailboxChannel) {
		case "url_api":
			if strings.TrimSpace(m.AccessKey) == "" {
				writeError(w, http.StatusUnprocessableEntity, "该账号未配置 url_api 邮件收码接口")
				return
			}
			payload, err = fetchURLAPILatestMail(m.Email, m.AccessKey, limit, proxyURL)
		case "xbovo":
			payload, err = fetchXbovoLatestMail(m.Email, m.AccessKey, limit, proxyURL)
		default:
			err = &outlookMailError{Code: "mailbox_channel_unsupported", Category: "format", HTTPStatus: http.StatusUnprocessableEntity, UserMessage: "暂不支持该 iCloud 邮箱渠道", Terminal: true}
		}
	} else {
		payload, err = fetchOutlookLatestMail(m.Email, m.ClientID, m.RefreshToken, limit, proxyURL)
	}
	if err != nil {
		s.db.Model(m).UpdateColumn("last_error", err.Error())
		mailErr := classifyOutlookMailError(err)
		writeJSON(w, mailErr.HTTPStatus, map[string]any{
			"code":     mailErr.Code,
			"category": mailErr.Category,
			"detail":   mailErr.UserMessage,
			"error":    mailErr.UserMessage,
		})
		return
	}
	if normalizeSunnyMailboxType(m.MailboxType) == "apple" && normalizeSunnyMailboxChannel(m.MailboxType, m.MailboxChannel) == "url_api" {
		decorateURLAPIPreviewPayload(payload, m.AccessKey, m.ID)
	}
	s.db.Model(m).UpdateColumns(map[string]any{
		"latest_mail_json": dumpJSON(payload),
		"last_mail_at":     time.Now(),
		"last_error":       "",
	})
	writeJSON(w, 200, payload)
}

func (s *Server) sunnyURLAPIPreview(w http.ResponseWriter, r *http.Request, m *SunnyMailbox) {
	if normalizeSunnyMailboxType(m.MailboxType) != "apple" || normalizeSunnyMailboxChannel(m.MailboxType, m.MailboxChannel) != "url_api" {
		http.Error(w, "This preview is only available for url_api iCloud mailboxes.", http.StatusUnprocessableEntity)
		return
	}
	page, err := fetchURLAPIPreviewHTML(m.AccessKey, strings.TrimSpace(r.URL.Query().Get("target")), s.sunnyMailboxProxyURL(), m.ID)
	if err != nil {
		mailErr := classifyOutlookMailError(err)
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(mailErr.HTTPStatus)
		_, _ = fmt.Fprintf(w, "<!doctype html><html><body style='font-family:system-ui;padding:32px;color:#b91c1c'><h3>%s</h3><p>%s</p></body></html>", html.EscapeString(mailErr.UserMessage), html.EscapeString(mailErr.Detail))
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; img-src data: http: https:; style-src 'unsafe-inline' http: https:; font-src data: http: https:; script-src 'unsafe-inline'; form-action 'none'; connect-src 'none'; frame-ancestors 'self'")
	_, _ = io.WriteString(w, page)
}

// sunnyMailboxProxyURL returns an empty proxy so auxiliary services use direct
// server egress. ChatGPT registration traffic selects a proxy-pool entry separately.
func (s *Server) sunnyMailboxProxyURL() string {
	return ""
}

func fetchOutlookLatestMail(email, clientID, refreshToken string, limit int, proxyURL string) (map[string]any, error) {
	if strings.TrimSpace(email) == "" || !strings.Contains(email, "@") || strings.TrimSpace(clientID) == "" || strings.TrimSpace(refreshToken) == "" {
		return nil, &outlookMailError{
			Code: "mailbox_format_error", Category: "format", HTTPStatus: http.StatusUnprocessableEntity,
			UserMessage: "邮箱凭证格式错误，应为 邮箱----密码----client_id----Refresh Token",
			Terminal:    true,
		}
	}
	// The imported four-field credential can be issued for Graph, legacy
	// IMAP/POP3, or an application that grants both. Detect the usable audience
	// instead of assuming every successful refresh token is an IMAP token.
	// Microsoft can issue a token from more than one compatible endpoint, while
	// IMAP accepts only one of them for certain legacy Outlook accounts. Pair
	// each refresh attempt with IMAP authentication instead of treating the
	// first token response as proof that it is usable for IMAP.
	errors := []string{}
	for _, endpoint := range hotmailGraphTokenEndpoints {
		token, err := refreshHotmailAccessTokenFromEndpoint(clientID, refreshToken, endpoint, proxyURL)
		if err != nil {
			if isTerminalOutlookMailError(err) {
				return nil, err
			}
			errors = append(errors, endpoint.Name+" token: "+err.Error())
			continue
		}
		items, err := fetchLatestMailsViaGraph(email, token, limit, proxyURL)
		if err == nil {
			return map[string]any{"email": email, "token_endpoint": endpoint.Name, "mail_protocol": "graph", "items": items, "count": len(items), "limit": limit}, nil
		}
		errors = append(errors, endpoint.Name+" Graph: "+err.Error())
	}
	for _, endpoint := range hotmailTokenEndpoints {
		token, err := refreshHotmailAccessTokenFromEndpoint(clientID, refreshToken, endpoint, proxyURL)
		if err != nil {
			if isTerminalOutlookMailError(err) {
				return nil, err
			}
			errors = append(errors, endpoint.Name+" token: "+err.Error())
			continue
		}
		items, err := fetchLatestMailsViaIMAP(email, token, limit, proxyURL)
		if err == nil {
			return map[string]any{"email": email, "token_endpoint": endpoint.Name, "mail_protocol": "imap", "items": items, "count": len(items), "limit": limit}, nil
		}
		errors = append(errors, endpoint.Name+" IMAP: "+err.Error())
	}
	return nil, newOutlookMailAggregateError(errors)
}

type hotmailTokenEndpoint struct {
	Name     string
	URL      string
	Scope    string
	Resource string
}

var hotmailTokenEndpoints = []hotmailTokenEndpoint{
	{Name: "LIVE", URL: "https://login.live.com/oauth20_token.srf"},
	{Name: "LIVE+scope", URL: "https://login.live.com/oauth20_token.srf", Scope: "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"},
	{Name: "V1-COMMON", URL: "https://login.microsoftonline.com/common/oauth2/token", Resource: "https://outlook.office.com/"},
	{Name: "V1-CONSUMERS", URL: "https://login.microsoftonline.com/consumers/oauth2/token", Resource: "https://outlook.office.com/"},
	{Name: "CONSUMERS", URL: "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", Scope: "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"},
	{Name: "CONSUMERS-noscope", URL: "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"},
	{Name: "COMMON", URL: "https://login.microsoftonline.com/common/oauth2/v2.0/token", Scope: "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"},
	{Name: "COMMON-noscope", URL: "https://login.microsoftonline.com/common/oauth2/v2.0/token"},
}

var hotmailGraphTokenEndpoints = []hotmailTokenEndpoint{
	{Name: "GRAPH-LIVE", URL: "https://login.live.com/oauth20_token.srf", Scope: "https://graph.microsoft.com/.default"},
	{Name: "GRAPH-CONSUMERS", URL: "https://login.microsoftonline.com/consumers/oauth2/v2.0/token", Scope: "https://graph.microsoft.com/.default"},
	{Name: "GRAPH-COMMON", URL: "https://login.microsoftonline.com/common/oauth2/v2.0/token", Scope: "https://graph.microsoft.com/.default"},
}

var outlookGraphMessagesURL = "https://graph.microsoft.com/v1.0/me/messages"

type hotmailAccessTokenCacheEntry struct {
	Token     string
	Endpoint  string
	ExpiresAt time.Time
}

var hotmailAccessTokenCache sync.Map

func hotmailAccessTokenCacheKey(email, clientID, refreshToken, proxyURL string) string {
	return strings.ToLower(strings.TrimSpace(email)) + "\x00" + strings.TrimSpace(clientID) + "\x00" + strings.TrimSpace(refreshToken) + "\x00" + strings.TrimSpace(proxyURL)
}

func refreshHotmailAccessTokenCached(email, clientID, refreshToken, proxyURL string) (string, string, error) {
	key := hotmailAccessTokenCacheKey(email, clientID, refreshToken, proxyURL)
	if value, ok := hotmailAccessTokenCache.Load(key); ok {
		if entry, ok := value.(hotmailAccessTokenCacheEntry); ok && entry.Token != "" && time.Now().Before(entry.ExpiresAt) {
			return entry.Token, entry.Endpoint, nil
		}
	}
	token, endpoint, err := refreshHotmailAccessToken(email, clientID, refreshToken, proxyURL)
	if err != nil {
		hotmailAccessTokenCache.Delete(key)
		return "", "", err
	}
	hotmailAccessTokenCache.Store(key, hotmailAccessTokenCacheEntry{
		Token:     token,
		Endpoint:  endpoint,
		ExpiresAt: time.Now().Add(50 * time.Minute),
	})
	return token, endpoint, nil
}

func refreshHotmailAccessTokenFromEndpoint(clientID, refreshToken string, ep hotmailTokenEndpoint, proxyURL string, meters ...*sunnyTrafficMeter) (string, error) {
	client := &http.Client{Timeout: 20 * time.Second}
	if proxyURL != "" {
		if u, err := url.Parse(proxyURL); err == nil {
			transport := &http.Transport{Proxy: http.ProxyURL(u)}
			if len(meters) > 0 && meters[0] != nil {
				client.Transport = &sunnyTrafficTransport{base: transport, meter: meters[0]}
			} else {
				client.Transport = transport
			}
		}
	}
	form := url.Values{}
	form.Set("client_id", clientID)
	form.Set("grant_type", "refresh_token")
	form.Set("refresh_token", refreshToken)
	if ep.Scope != "" {
		form.Set("scope", ep.Scope)
	}
	if ep.Resource != "" {
		form.Set("resource", ep.Resource)
	}
	req, _ := http.NewRequest(http.MethodPost, ep.URL, strings.NewReader(form.Encode()))
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	resp, err := client.Do(req)
	if err != nil {
		return "", &outlookMailError{
			Code: "mailbox_network_error", Category: "network", HTTPStatus: http.StatusServiceUnavailable,
			UserMessage: "邮箱服务网络连接失败，请检查服务器出网、代理与 Microsoft 服务连通性",
			Detail:      err.Error(),
		}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	var payload map[string]any
	_ = json.Unmarshal(raw, &payload)
	if resp.StatusCode >= 200 && resp.StatusCode < 300 && text(payload["access_token"]) != "" {
		return text(payload["access_token"]), nil
	}
	return "", newOutlookTokenError(resp.StatusCode, payload)
}

func refreshHotmailAccessToken(email, clientID, refreshToken, proxyURL string) (string, string, error) {
	_ = email
	errors := []string{}
	for _, ep := range hotmailTokenEndpoints {
		token, err := refreshHotmailAccessTokenFromEndpoint(clientID, refreshToken, ep, proxyURL)
		if err == nil {
			return token, ep.Name, nil
		}
		if isTerminalOutlookMailError(err) {
			return "", "", err
		}
		errors = append(errors, ep.Name+": "+err.Error())
	}
	return "", "", newOutlookMailAggregateError(errors)
}

func fetchLatestMailsViaGraph(emailAddr, accessToken string, limit int, proxyURL string) ([]map[string]any, error) {
	if limit < 1 {
		limit = 5
	}
	if limit > 50 {
		limit = 50
	}
	endpoint, err := url.Parse(outlookGraphMessagesURL)
	if err != nil {
		return nil, fmt.Errorf("invalid Graph messages URL: %w", err)
	}
	query := endpoint.Query()
	query.Set("$top", strconv.Itoa(limit))
	query.Set("$orderby", "receivedDateTime desc")
	query.Set("$select", "id,subject,from,toRecipients,receivedDateTime,bodyPreview,body,isRead")
	endpoint.RawQuery = query.Encode()

	client := &http.Client{Timeout: 25 * time.Second}
	if strings.TrimSpace(proxyURL) != "" {
		proxy, parseErr := url.Parse(proxyURL)
		if parseErr != nil {
			return nil, fmt.Errorf("invalid Graph proxy URL: %w", parseErr)
		}
		client.Transport = &http.Transport{Proxy: http.ProxyURL(proxy)}
	}
	req, err := http.NewRequest(http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("Authorization", "Bearer "+accessToken)
	req.Header.Set("Prefer", `outlook.body-content-type="html"`)
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Graph request failed: %w", err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, fmt.Errorf("Graph response read failed: %w", err)
	}
	var payload map[string]any
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, fmt.Errorf("Graph returned invalid JSON (HTTP %d)", resp.StatusCode)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		detail := ""
		if graphError, ok := payload["error"].(map[string]any); ok {
			detail = strings.TrimSpace(strings.Join([]string{text(graphError["code"]), text(graphError["message"])}, ": "))
		}
		return nil, fmt.Errorf("Graph HTTP %d: %s", resp.StatusCode, fallback(detail, string(raw[:min(len(raw), 300)])))
	}
	values, _ := payload["value"].([]any)
	items := make([]map[string]any, 0, len(values))
	for _, rawMessage := range values {
		message, ok := rawMessage.(map[string]any)
		if ok {
			items = append(items, graphMailItem(emailAddr, message))
		}
	}
	return items, nil
}

func graphMailItem(emailAddr string, message map[string]any) map[string]any {
	sender := ""
	if from, ok := message["from"].(map[string]any); ok {
		if address, ok := from["emailAddress"].(map[string]any); ok {
			name, emailAddress := text(address["name"]), text(address["address"])
			sender = emailAddress
			if name != "" {
				sender = name
				if emailAddress != "" {
					sender += " <" + emailAddress + ">"
				}
			}
		}
	}
	recipients := []string{}
	if values, ok := message["toRecipients"].([]any); ok {
		for _, rawRecipient := range values {
			recipient, _ := rawRecipient.(map[string]any)
			address, _ := recipient["emailAddress"].(map[string]any)
			if value := text(address["address"]); value != "" {
				recipients = append(recipients, value)
			}
		}
	}
	bodyRaw, bodyType := text(message["bodyPreview"]), "text"
	if body, ok := message["body"].(map[string]any); ok {
		bodyRaw = fallback(text(body["content"]), bodyRaw)
		bodyType = strings.ToLower(text(body["contentType"]))
	}
	bodyText := bodyRaw
	if bodyType == "html" {
		bodyText = html.UnescapeString(regexp.MustCompile(`<[^>]+>`).ReplaceAllString(bodyRaw, " "))
	}
	bodyText = strings.TrimSpace(regexp.MustCompile(`\s+`).ReplaceAllString(bodyText, " "))
	subject := text(message["subject"])
	otp := ""
	for _, pattern := range []string{`(?i)(?:OpenAI|ChatGPT|verification|verify|code)[^\d]{0,120}(\d{6})`, `\b(\d{6})\b`} {
		if match := regexp.MustCompile(pattern).FindStringSubmatch(subject + "\n" + bodyText); len(match) > 1 {
			otp = match[1]
			break
		}
	}
	return map[string]any{
		"id": text(message["id"]), "email": emailAddr, "subject": subject, "from": sender,
		"to": strings.Join(recipients, ", "), "date": text(message["receivedDateTime"]),
		"body": bodyText, "body_preview": fallback(text(message["bodyPreview"]), bodyText[:min(len(bodyText), 1200)]),
		"raw_html": bodyRaw, "otp": otp, "source": "graph",
	}
}

func dialOutlookIMAPS(proxyURL string) (*tls.Conn, error) {
	const host = "outlook.office365.com"
	const target = host + ":993"
	const timeout = 30 * time.Second
	if strings.TrimSpace(proxyURL) == "" {
		return tls.DialWithDialer(&net.Dialer{Timeout: timeout}, "tcp", target, &tls.Config{ServerName: host})
	}

	proxy, err := url.Parse(proxyURL)
	if err != nil || proxy.Hostname() == "" {
		return nil, fmt.Errorf("invalid IMAP proxy URL")
	}
	if scheme := strings.ToLower(proxy.Scheme); scheme != "http" && scheme != "https" {
		return nil, fmt.Errorf("IMAP proxy only supports HTTP CONNECT: %s", proxy.Scheme)
	}
	port := proxy.Port()
	if port == "" {
		port = "80"
	}
	raw, err := (&net.Dialer{Timeout: timeout}).Dial("tcp", net.JoinHostPort(proxy.Hostname(), port))
	if err != nil {
		return nil, fmt.Errorf("IMAP proxy dial failed: %w", err)
	}
	closeRaw := true
	defer func() {
		if closeRaw {
			_ = raw.Close()
		}
	}()
	_ = raw.SetDeadline(time.Now().Add(timeout))

	request := []string{
		"CONNECT " + target + " HTTP/1.1",
		"Host: " + target,
		"Proxy-Connection: keep-alive",
		"User-Agent: SunnyRegister/1.0",
	}
	if proxy.User != nil {
		password, _ := proxy.User.Password()
		auth := base64.StdEncoding.EncodeToString([]byte(proxy.User.Username() + ":" + password))
		request = append(request, "Proxy-Authorization: Basic "+auth)
	}
	if _, err := io.WriteString(raw, strings.Join(request, "\r\n")+"\r\n\r\n"); err != nil {
		return nil, fmt.Errorf("IMAP proxy CONNECT write failed: %w", err)
	}
	// Keep one buffered reader for the whole CONNECT response: creating a new
	// reader after the status line could discard headers already buffered by the
	// first reader.
	reader := bufio.NewReader(raw)
	response, err := reader.ReadString('\n')
	if err != nil {
		return nil, fmt.Errorf("IMAP proxy CONNECT response failed: %w", err)
	}
	if !strings.Contains(response, " 200 ") {
		return nil, fmt.Errorf("IMAP proxy CONNECT failed: %s", strings.TrimSpace(response))
	}
	// Consume the remaining HTTP response headers before beginning TLS.
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return nil, fmt.Errorf("IMAP proxy CONNECT headers failed: %w", err)
		}
		if line == "\r\n" || line == "\n" {
			break
		}
	}
	_ = raw.SetDeadline(time.Time{})
	conn := tls.Client(raw, &tls.Config{ServerName: host})
	if err := conn.Handshake(); err != nil {
		return nil, fmt.Errorf("IMAP TLS handshake via proxy failed: %w", err)
	}
	closeRaw = false
	return conn, nil
}

func fetchLatestMailViaIMAP(emailAddr, accessToken string) (map[string]any, error) {
	items, err := fetchLatestMailsViaIMAP(emailAddr, accessToken, 1, "")
	if err != nil {
		return nil, err
	}
	if len(items) == 0 {
		return map[string]any{"empty": true}, nil
	}
	return items[0], nil
}

func fetchLatestMailsViaIMAP(emailAddr, accessToken string, limit int, proxyURL string) ([]map[string]any, error) {
	if limit < 1 {
		limit = 5
	}
	if limit > 50 {
		limit = 50
	}
	conn, err := dialOutlookIMAPS(proxyURL)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	reader := bufio.NewReader(conn)
	if _, err := reader.ReadString('\n'); err != nil {
		return nil, fmt.Errorf("IMAP greeting failed: %w", err)
	}
	write := func(format string, args ...any) error {
		_, err := fmt.Fprintf(conn, format+"\r\n", args...)
		return err
	}
	readUntil := func(tag string) (string, error) {
		var b strings.Builder
		for {
			line, err := reader.ReadString('\n')
			if err != nil {
				return b.String(), err
			}
			b.WriteString(line)
			if strings.HasPrefix(line, tag+" ") {
				return b.String(), nil
			}
		}
	}
	auth := base64.StdEncoding.EncodeToString([]byte(fmt.Sprintf("user=%s\x01auth=Bearer %s\x01\x01", emailAddr, accessToken)))
	if err := write("A1 AUTHENTICATE XOAUTH2 %s", auth); err != nil {
		return nil, err
	}
	// Outlook may issue a SASL error continuation before it sends A1 NO. Send
	// the required empty response so rejected candidates fail immediately rather
	// than leaving the request blocked until the socket timeout.
	var authOut strings.Builder
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return nil, fmt.Errorf("IMAP XOAUTH2 response failed: %w", err)
		}
		authOut.WriteString(line)
		if strings.HasPrefix(line, "+") {
			if err := write(""); err != nil {
				return nil, err
			}
			continue
		}
		if strings.HasPrefix(line, "A1 ") {
			break
		}
	}
	if !strings.Contains(authOut.String(), "A1 OK") {
		return nil, fmt.Errorf("IMAP XOAUTH2 authentication failed: %s", strings.TrimSpace(authOut.String()))
	}
	if err := write("A2 SELECT INBOX"); err != nil {
		return nil, err
	}
	selectOut, err := readUntil("A2")
	if err != nil || !strings.Contains(selectOut, "A2 OK") {
		return nil, fmt.Errorf("IMAP SELECT INBOX 失败: %s", strings.TrimSpace(selectOut))
	}
	totalMessages := 0
	for _, line := range strings.Split(selectOut, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "* ") && strings.HasSuffix(line, " EXISTS") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				totalMessages = toInt(fields[1])
				break
			}
		}
	}
	if totalMessages <= 0 {
		return []map[string]any{}, nil
	}
	startSeq := totalMessages - limit + 1
	if startSeq < 1 {
		startSeq = 1
	}
	const maxIMAPMailBytes = 384 * 1024
	tag := "A3"
	if err := write("%s FETCH %d:%d BODY.PEEK[]<0.%d>", tag, startSeq, totalMessages, maxIMAPMailBytes); err != nil {
		return nil, err
	}
	rawBatch, err := readUntil(tag)
	if err != nil {
		return nil, err
	}
	items := []map[string]any{}
	for seq := totalMessages; seq >= startSeq; seq-- {
		msgID := strconv.Itoa(seq)
		raw := extractIMAPFetchFragment(rawBatch, seq, totalMessages, tag)
		if raw == "" {
			continue
		}
		item := parseIMAPMailItem(msgID, raw, tag)
		if item != nil {
			item["email"] = emailAddr
			items = append(items, item)
		}
	}
	return items, nil
}

func extractIMAPFetchFragment(raw string, seq, endSeq int, tag string) string {
	marker := fmt.Sprintf("* %d FETCH", seq)
	start := strings.Index(raw, marker)
	if start < 0 {
		marker = "\r\n" + marker
		start = strings.Index(raw, marker)
		if start >= 0 {
			start += 2
		}
	}
	if start < 0 {
		return ""
	}
	end := len(raw)
	for next := seq + 1; next <= endSeq; next++ {
		nextMarker := fmt.Sprintf("\r\n* %d FETCH", next)
		if pos := strings.Index(raw[start+1:], nextMarker); pos >= 0 {
			end = start + 1 + pos
			break
		}
	}
	if tagPos := strings.LastIndex(raw, "\r\n"+tag+" "); tagPos >= 0 && tagPos > start && tagPos < end {
		end = tagPos
	}
	if end <= start {
		return ""
	}
	return raw[start:end] + "\r\n" + tag + " OK\r\n"
}

func parseIMAPMailItem(msgID, raw, tag string) map[string]any {
	start := strings.Index(raw, "\r\n")
	end := strings.LastIndex(raw, "\r\n"+tag+" ")
	if start >= 0 && end > start {
		raw = raw[start+2 : end]
	}
	m, err := mail.ReadMessage(strings.NewReader(raw))
	if err != nil {
		return map[string]any{"id": msgID, "parse_error": err.Error(), "raw_preview": strings.TrimSpace(raw[:min(len(raw), 1200)])}
	}
	decoder := &mime.WordDecoder{CharsetReader: mailCharsetReader}
	subject := m.Header.Get("Subject")
	if dec, derr := decoder.DecodeHeader(subject); derr == nil {
		subject = dec
	}
	from := m.Header.Get("From")
	if dec, derr := decoder.DecodeHeader(from); derr == nil {
		from = dec
	}
	bodyText, bodyHTML := extractMailBodies(textproto.MIMEHeader(m.Header), m.Body)
	bodyRaw := bodyHTML
	if bodyRaw == "" {
		bodyRaw = bodyText
	}
	if bodyText == "" && bodyHTML != "" {
		bodyText = html.UnescapeString(regexp.MustCompile(`<[^>]+>`).ReplaceAllString(bodyHTML, " "))
	}
	bodyText = strings.TrimSpace(regexp.MustCompile(`\s+`).ReplaceAllString(bodyText, " "))
	otp := ""
	for _, pat := range []string{`(?i)(?:OpenAI|ChatGPT|verification|verify|code)[^\d]{0,120}(\d{6})`, `\b(\d{6})\b`} {
		if match := regexp.MustCompile(pat).FindStringSubmatch(subject + "\n" + bodyText); len(match) > 1 {
			otp = match[1]
			break
		}
	}
	return map[string]any{
		"id": msgID, "subject": subject, "from": from, "to": m.Header.Get("To"),
		"date": m.Header.Get("Date"), "body": bodyText, "body_preview": strings.TrimSpace(bodyText[:min(len(bodyText), 1200)]),
		"raw_html": bodyRaw, "otp": otp,
	}
}

func mailCharsetReader(charset string, input io.Reader) (io.Reader, error) {
	encoding, err := htmlindex.Get(strings.TrimSpace(charset))
	if err != nil {
		return nil, err
	}
	return encoding.NewDecoder().Reader(input), nil
}

func decodeMailBytes(raw []byte, charset string) string {
	charset = strings.TrimSpace(charset)
	if charset == "" || strings.EqualFold(charset, "utf-8") || strings.EqualFold(charset, "us-ascii") {
		return string(raw)
	}
	encoding, err := htmlindex.Get(charset)
	if err != nil {
		return string(raw)
	}
	decoded, err := encoding.NewDecoder().Bytes(raw)
	if err != nil {
		return string(raw)
	}
	return string(decoded)
}

func htmlDeclaredCharset(raw []byte) string {
	// Some legacy mail omits charset in Content-Type but provides it in an HTML
	// meta tag. Decode that declaration before handing HTML to the browser.
	match := regexp.MustCompile(`(?i)<meta[^>]+charset\s*=\s*["']?\s*([a-z0-9._-]+)`).FindSubmatch(raw)
	if len(match) > 1 {
		return string(match[1])
	}
	match = regexp.MustCompile(`(?i)<meta[^>]+content\s*=\s*["'][^"']*charset\s*=\s*([a-z0-9._-]+)`).FindSubmatch(raw)
	if len(match) > 1 {
		return string(match[1])
	}
	return ""
}

func extractMailBodies(header textproto.MIMEHeader, body io.Reader) (string, string) {
	mediaType, params, _ := mime.ParseMediaType(header.Get("Content-Type"))
	mediaType = strings.ToLower(mediaType)
	if strings.HasPrefix(mediaType, "multipart/") && params["boundary"] != "" {
		mr := multipart.NewReader(body, params["boundary"])
		texts, htmls := []string{}, []string{}
		for {
			part, err := mr.NextPart()
			if err != nil {
				break
			}
			t, h := extractMailBodies(part.Header, part)
			if t != "" {
				texts = append(texts, t)
			}
			if h != "" {
				htmls = append(htmls, h)
			}
		}
		return strings.Join(texts, "\n"), strings.Join(htmls, "\n")
	}
	// Attachments such as PNG/PDF must not be decoded as mail text; doing so
	// produced binary garbage in the preview.
	if mediaType != "text/plain" && mediaType != "text/html" {
		return "", ""
	}
	reader := body
	switch strings.ToLower(strings.TrimSpace(header.Get("Content-Transfer-Encoding"))) {
	case "quoted-printable":
		reader = quotedprintable.NewReader(body)
	case "base64":
		reader = base64.NewDecoder(base64.StdEncoding, body)
	}
	raw, _ := io.ReadAll(io.LimitReader(reader, 4<<20))
	charset := params["charset"]
	if charset == "" && mediaType == "text/html" {
		charset = htmlDeclaredCharset(raw)
	}
	value := decodeMailBytes(raw, charset)
	if mediaType == "text/html" {
		return "", value
	}
	return value, ""
}

func defaultPhoneConfig() map[string]any {
	return map[string]any{
		"pool_enabled":             true,
		"luban_enabled":            false,
		"luban_base_url":           "https://lubansms.com/v2/api/",
		"luban_api_key":            "",
		"luban_service_id":         "",
		"smsbower_enabled":         false,
		"smsbower_base_url":        "https://smsbower.page/stubs/handler_api.php",
		"smsbower_api_key":         "",
		"smsbower_default_country": "187",
		"smsbower_default_service": "dr",
		"smsbower_max_price":       -1,
		"smspool_enabled":          false,
		"smspool_base_url":         "https://api.smspool.net",
		"smspool_api_key":          "",
		"smspool_default_country":  "1",
		"smspool_default_service":  "671",
		"smspool_max_price":        -1,
		"firefox_enabled":          false,
		"firefox_base_url":         fireFoxAPIURL,
		"firefox_api_token":        "",
		"firefox_api_name":         "",
		"firefox_password":         "",
		"firefox_default_country":  "usa",
		"firefox_default_service":  "1096",
		"firefox_max_price":        0,
	}
}
func defaultMailboxConfig() map[string]any { return map[string]any{"pool_enabled": true} }

func (s *Server) sunnyPhones(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 1 && parts[0] == "config" && r.Method == http.MethodGet {
		cfg := s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig())
		cfg["firefox_api_token"] = fireFoxAPIToken(cfg)
		cfg["firefox_api_name"] = ""
		cfg["firefox_password"] = ""
		cfg["usable_count"] = s.sunnyUsablePhoneCount()
		cfg["total_count"] = s.sunnyPhoneTotalCount()
		writeJSON(w, 200, cfg)
		return
	}
	if len(parts) == 1 && parts[0] == "config" && r.Method == http.MethodPut {
		body, _ := parseBody(r)
		cfg := mergeConfig(defaultPhoneConfig(), body)
		cfg["firefox_api_token"] = fireFoxAPIToken(cfg)
		cfg["firefox_api_name"] = ""
		cfg["firefox_password"] = ""
		s.sunnySaveConfig(sunnyCfgPhone, cfg)
		writeJSON(w, 200, s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig()))
		return
	}
	if len(parts) == 2 && parts[0] == "smsbower" && parts[1] == "check" && r.Method == http.MethodPost {
		s.sunnyCheckSMSBower(w, r)
		return
	}
	if len(parts) == 2 && parts[0] == "luban" && parts[1] == "check" && r.Method == http.MethodPost {
		s.sunnyCheckLubanSMS(w, r)
		return
	}
	if len(parts) == 2 && parts[0] == "smspool" && parts[1] == "check" && r.Method == http.MethodPost {
		s.sunnyCheckSMSPool(w, r)
		return
	}
	if len(parts) == 2 && parts[0] == "firefox" && parts[1] == "check" && r.Method == http.MethodPost {
		s.sunnyCheckFireFox(w, r)
		return
	}
	if len(parts) == 1 && parts[0] == "provider-options" && (r.Method == http.MethodGet || r.Method == http.MethodPost) {
		s.sunnySMSProviderOptions(w, r)
		return
	}
	if len(parts) == 0 && r.Method == http.MethodGet {
		var rows []SunnyPhone
		page := intValue(r.URL.Query().Get("page"), 1)
		if page < 1 {
			page = 1
		}
		pageSize := intValue(r.URL.Query().Get("page_size"), 10)
		if pageSize < 1 {
			pageSize = 10
		}
		if pageSize > 100 {
			pageSize = 100
		}
		q := strings.TrimSpace(r.URL.Query().Get("q"))
		status := strings.TrimSpace(r.URL.Query().Get("status"))
		countFilter := strings.TrimSpace(r.URL.Query().Get("count"))
		query := s.db.Model(&SunnyPhone{})
		if q != "" {
			query = query.Where("number LIKE ?", "%"+q+"%")
		}
		switch status {
		case "enabled", "available":
			query = query.Where("enabled = ?", true)
		case "disabled":
			query = query.Where("enabled = ? OR status = ?", false, "disabled")
		}
		if countFilter != "" && countFilter != "all" {
			count := intValue(countFilter, -1)
			if count >= 0 && count <= 3 {
				query = query.Where("success_count = ?", count)
			}
		}
		if strings.EqualFold(strings.TrimSpace(r.URL.Query().Get("selection")), "all") {
			var ids []uint
			query.Order("id desc").Pluck("id", &ids)
			writeJSON(w, 200, map[string]any{"ids": ids, "total": len(ids)})
			return
		}
		var total int64
		query.Count(&total)
		query.Order(sunnySortClause(r.URL.Query().Get("sort_by"), r.URL.Query().Get("sort_order"), map[string]string{"last_used_at": "last_used_at", "updated_at": "updated_at", "created_at": "created_at", "cooldown_until": "cooldown_until"}, "id desc")).Limit(pageSize).Offset((page - 1) * pageSize).Find(&rows)
		items := make([]map[string]any, 0, len(rows))
		for _, row := range rows {
			items = append(items, serializeSunnyPhone(row))
		}
		writeJSON(w, 200, map[string]any{"items": items, "total": total, "page": page, "page_size": pageSize, "now": formatTime(time.Now())})
		return
	}
	if len(parts) == 0 && r.Method == http.MethodPost {
		body, _ := parseBody(r)
		p, err := sunnyPhoneFromBody(body)
		if err != nil {
			writeError(w, 400, err.Error())
			return
		}
		if err := s.db.Create(&p).Error; err != nil {
			writeError(w, 400, err.Error())
			return
		}
		writeJSON(w, 200, serializeSunnyPhone(p))
		return
	}
	if len(parts) == 1 && parts[0] == "import" && r.Method == http.MethodPost {
		body := s.sunnyReadImportBody(r)
		ok, bad := 0, []string{}
		for _, line := range strings.Split(text(body["lines"]), "\n") {
			if strings.TrimSpace(line) == "" {
				continue
			}
			p, err := parseSunnyPhoneLine(line)
			if err != nil {
				bad = append(bad, line+" => "+err.Error())
				continue
			}
			var old SunnyPhone
			if err := s.db.First(&old, "number = ?", p.Number).Error; err == nil {
				p.ID, p.CreatedAt, p.SuccessCount, p.CooldownUntil, p.LastUsedAt = old.ID, old.CreatedAt, old.SuccessCount, old.CooldownUntil, old.LastUsedAt
				s.db.Save(&p)
			} else {
				s.db.Create(&p)
			}
			ok++
		}
		writeJSON(w, 200, map[string]any{"ok": true, "imported": ok, "failed": len(bad), "errors": bad})
		return
	}
	if len(parts) == 1 {
		id := uint(intValue(parts[0], 0))
		var p SunnyPhone
		if id == 0 || s.db.First(&p, id).Error != nil {
			writeError(w, 404, "phone not found")
			return
		}
		if r.Method == http.MethodPut {
			body, _ := parseBody(r)
			next, err := sunnyPhoneFromBody(body)
			if err != nil {
				writeError(w, 400, err.Error())
				return
			}
			next.ID, next.CreatedAt, next.CooldownUntil, next.LastUsedAt, next.LastCode, next.LastError = p.ID, p.CreatedAt, p.CooldownUntil, p.LastUsedAt, p.LastCode, p.LastError
			s.db.Save(&next)
			writeJSON(w, 200, serializeSunnyPhone(next))
			return
		}
		if r.Method == http.MethodDelete {
			s.db.Delete(&p)
			writeJSON(w, 200, map[string]any{"ok": true})
			return
		}
	}
	writeError(w, 404, "not found")
}

func (s *Server) sunnyCheckSMSBower(w http.ResponseWriter, r *http.Request) {
	body, _ := parseBody(r)
	cfg := mergeConfig(s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig()), body)
	apiKey := strings.TrimSpace(text(cfg["smsbower_api_key"]))
	if apiKey == "" {
		writeError(w, 400, "SMSBower API Key is required")
		return
	}
	baseURL := strings.TrimSpace(text(cfg["smsbower_base_url"]))
	if baseURL == "" {
		baseURL = "https://smsbower.page/stubs/handler_api.php"
	}
	params := url.Values{}
	params.Set("api_key", apiKey)
	params.Set("action", "getBalance")
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, baseURL+"?"+params.Encode(), nil)
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	client := &http.Client{Timeout: 20 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	raw := strings.TrimSpace(string(b))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		writeError(w, 400, fmt.Sprintf("SMSBower HTTP %d: %s", resp.StatusCode, raw))
		return
	}
	if !strings.HasPrefix(raw, "ACCESS_BALANCE:") {
		writeError(w, 400, raw)
		return
	}
	balance := strings.TrimPrefix(raw, "ACCESS_BALANCE:")
	writeJSON(w, 200, map[string]any{"ok": true, "balance": balance, "raw": raw})
}

func (s *Server) sunnyCheckLubanSMS(w http.ResponseWriter, r *http.Request) {
	body, _ := parseBody(r)
	cfg := mergeConfig(s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig()), body)
	apiKey := strings.TrimSpace(text(cfg["luban_api_key"]))
	serviceID := strings.TrimSpace(text(cfg["luban_service_id"]))
	if apiKey == "" || serviceID == "" {
		writeError(w, 400, "LubanSMS API Key and service ID are required")
		return
	}
	if !regexp.MustCompile(`^[A-Za-z0-9._:-]{1,80}$`).MatchString(serviceID) {
		writeError(w, 400, "LubanSMS service ID is invalid")
		return
	}
	baseURL := strings.TrimRight(strings.TrimSpace(text(cfg["luban_base_url"])), "/")
	if baseURL == "" {
		baseURL = "https://lubansms.com/v2/api"
	}
	params := url.Values{"apikey": {apiKey}, "request_id": {"sunnyregister-connectivity-check"}}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, baseURL+"/getSms?"+params.Encode(), nil)
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	data := jsonMap(string(raw))
	providerCode := intValue(data["code"], 0)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 || len(data) == 0 || providerCode == 400 || providerCode == 401 {
		writeError(w, 400, fmt.Sprintf("LubanSMS check failed: HTTP %d %s", resp.StatusCode, strings.TrimSpace(string(raw))))
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "message": "LubanSMS API is reachable", "service_id": serviceID})
}

func (s *Server) sunnyCheckSMSPool(w http.ResponseWriter, r *http.Request) {
	body, _ := parseBody(r)
	cfg := mergeConfig(s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig()), body)
	apiKey := strings.TrimSpace(text(cfg["smspool_api_key"]))
	if apiKey == "" {
		writeError(w, 400, "SMSPool API Key is required")
		return
	}
	baseURL := strings.TrimRight(strings.TrimSpace(text(cfg["smspool_base_url"])), "/")
	if baseURL == "" {
		baseURL = "https://api.smspool.net"
	}
	raw, status, err := postSunnyMultipart(r.Context(), baseURL+"/request/balance", apiKey, map[string]string{"key": apiKey})
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	if status < 200 || status >= 300 {
		writeError(w, 400, fmt.Sprintf("SMSPool HTTP %d: %s", status, raw))
		return
	}
	data := jsonMap(raw)
	balance := strings.TrimSpace(text(data["balance"]))
	if balance == "" {
		if msg := firstText(data["message"], data["type"]); msg != "" {
			writeError(w, 400, msg)
			return
		}
		balance = raw
	}
	writeJSON(w, 200, map[string]any{"ok": true, "balance": balance, "raw": raw})
}

func (s *Server) sunnyCheckFireFox(w http.ResponseWriter, r *http.Request) {
	body, _ := parseBody(r)
	cfg := mergeConfig(s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig()), body)
	token := fireFoxAPIToken(cfg)
	if token == "" {
		writeError(w, http.StatusBadRequest, "FireFox API Token is required")
		return
	}
	baseURL := strings.TrimSpace(text(cfg["firefox_base_url"]))
	infoRaw, err := getFireFoxAPI(r.Context(), baseURL, url.Values{
		"act":   {"myInfo"},
		"token": {token},
	})
	if err != nil {
		writeError(w, http.StatusBadRequest, err.Error())
		return
	}
	infoParts := strings.Split(infoRaw, "|")
	if len(infoParts) < 2 || strings.TrimSpace(infoParts[0]) != "1" {
		writeError(w, http.StatusBadRequest, fireFoxFailureMessage("myInfo", infoRaw))
		return
	}
	writeJSON(w, 200, map[string]any{"ok": true, "balance": strings.TrimSpace(infoParts[1]), "raw": infoRaw})
}

func getFireFoxAPI(ctx context.Context, baseURL string, params url.Values) (string, error) {
	normalized, err := normalizeFireFoxAPIURL(baseURL)
	if err != nil {
		return "", err
	}
	target, err := url.Parse(normalized)
	if err != nil {
		return "", err
	}
	query := target.Query()
	for key, values := range params {
		for _, value := range values {
			query.Set(key, value)
		}
	}
	target.RawQuery = query.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target.String(), nil)
	if err != nil {
		return "", err
	}
	resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	raw := strings.TrimSpace(strings.TrimPrefix(string(b), "\ufeff"))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("FireFox HTTP %d: %s", resp.StatusCode, raw[:min(len(raw), 500)])
	}
	if raw == "" {
		return "", fmt.Errorf("FireFox returned an empty response")
	}
	return raw, nil
}

func normalizeFireFoxAPIURL(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		raw = fireFoxAPIURL
	}
	target, err := url.Parse(raw)
	if err != nil || target.Scheme == "" || target.Host == "" {
		return "", fmt.Errorf("FireFox API URL is invalid")
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		return "", fmt.Errorf("FireFox API URL must use HTTP or HTTPS")
	}
	hostname := strings.ToLower(target.Hostname())
	if hostname == "www.firefox.fun" || hostname == "web.firefox.fun" {
		target.Scheme = "https"
	}
	path := strings.TrimRight(target.Path, "/")
	if path == "" {
		path = "/yhapi.ashx"
	} else if !strings.HasSuffix(strings.ToLower(path), "/yhapi.ashx") {
		path += "/yhapi.ashx"
	}
	target.Path = path
	target.RawPath = ""
	target.RawQuery = ""
	target.Fragment = ""
	return target.String(), nil
}

func fireFoxAPIToken(cfg map[string]any) string {
	if token := strings.TrimSpace(text(cfg["firefox_api_token"])); token != "" {
		return token
	}
	// Versions before API Token authentication stored this value in the
	// misleading firefox_password field.
	return strings.TrimSpace(text(cfg["firefox_password"]))
}

func fireFoxFailureMessage(action, raw string) string {
	parts := strings.Split(strings.TrimSpace(raw), "|")
	code := strings.TrimSpace(raw)
	if len(parts) >= 2 && strings.TrimSpace(parts[0]) == "0" {
		code = strings.TrimSpace(parts[1])
	}
	messages := map[string]map[string]string{
		"myInfo": {
			"-1": "token is missing",
			"-2": "token is invalid; update the API Token in FireFox settings",
			"-3": "balance can only be checked once every 60 seconds",
		},
	}
	label := map[string]string{"myInfo": "account check"}[action]
	if label == "" {
		label = action
	}
	message := messages[action][code]
	if message == "" {
		message = strings.TrimSpace(raw)
	}
	return fmt.Sprintf("FireFox %s failed (%s): %s", label, code, message)
}

func postFireFoxForm(ctx context.Context, baseURL, endpoint string, form url.Values) (string, error) {
	normalized, err := normalizeFireFoxAPIURL(baseURL)
	if err != nil {
		return "", err
	}
	target, err := url.Parse(normalized)
	if err != nil {
		return "", err
	}
	target.Path = endpoint
	target.RawPath = ""
	target.RawQuery = ""
	body := strings.NewReader(form.Encode())
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, target.String(), body)
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json, text/plain, */*")
	resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	raw := strings.TrimSpace(strings.TrimPrefix(string(b), "\ufeff"))
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("FireFox HTTP %d: %s", resp.StatusCode, raw[:min(len(raw), 500)])
	}
	if raw == "" {
		return "", fmt.Errorf("FireFox returned an empty response")
	}
	return raw, nil
}

func postSunnyMultipart(ctx context.Context, targetURL string, apiKey string, fields map[string]string) (string, int, error) {
	var requestBody bytes.Buffer
	writer := multipart.NewWriter(&requestBody)
	for k, v := range fields {
		if err := writer.WriteField(k, v); err != nil {
			return "", 0, err
		}
	}
	if err := writer.Close(); err != nil {
		return "", 0, err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, targetURL, &requestBody)
	if err != nil {
		return "", 0, err
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())
	req.Header.Set("Accept", "application/json")
	if apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+apiKey)
	}
	resp, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		return "", 0, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return strings.TrimSpace(string(b)), resp.StatusCode, nil
}

func (s *Server) sunnySMSProviderOptions(w http.ResponseWriter, r *http.Request) {
	body := map[string]any{}
	if r.Method == http.MethodPost {
		body, _ = parseBody(r)
	}
	q := r.URL.Query()
	provider := strings.ToLower(strings.TrimSpace(fallback(text(body["provider"]), q.Get("provider"))))
	kind := strings.ToLower(strings.TrimSpace(fallback(text(body["kind"]), q.Get("kind"))))
	parent := strings.TrimSpace(fallback(text(body["country"]), q.Get("country")))
	refresh := boolValue(firstText(body["refresh"], q.Get("refresh")), false)
	if provider != "smsbower" && provider != "smspool" && provider != "firefox" {
		writeError(w, 400, "invalid sms provider")
		return
	}
	if kind != "countries" && kind != "services" {
		writeError(w, 400, "invalid option kind")
		return
	}
	cacheKind := map[string]string{"countries": "country", "services": "service"}[kind]
	cfg := mergeConfig(s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig()), body)
	items, cached, err := s.sunnyLoadSMSProviderOptions(r.Context(), provider, cacheKind, parent, cfg, refresh)
	if err != nil {
		if len(items) > 0 {
			writeJSON(w, 200, map[string]any{"items": items, "cached": cached, "warning": err.Error()})
			return
		}
		writeError(w, 400, err.Error())
		return
	}
	writeJSON(w, 200, map[string]any{"items": items, "cached": cached})
}

type sunnySMSOptionsFlight struct {
	done   chan struct{}
	items  []map[string]any
	cached bool
	err    error
}

func (s *Server) sunnyLoadSMSProviderOptions(ctx context.Context, provider, kind, parent string, cfg map[string]any, refresh bool) ([]map[string]any, bool, error) {
	key := strings.Join([]string{provider, kind, parent}, "\x00")
	s.smsOptionsMu.Lock()
	if s.smsOptionsRun == nil {
		s.smsOptionsRun = map[string]*sunnySMSOptionsFlight{}
	}
	if running := s.smsOptionsRun[key]; running != nil {
		s.smsOptionsMu.Unlock()
		select {
		case <-running.done:
			return running.items, running.cached, running.err
		case <-ctx.Done():
			return nil, false, ctx.Err()
		}
	}
	running := &sunnySMSOptionsFlight{done: make(chan struct{})}
	s.smsOptionsRun[key] = running
	s.smsOptionsMu.Unlock()

	defer func() {
		s.smsOptionsMu.Lock()
		delete(s.smsOptionsRun, key)
		close(running.done)
		s.smsOptionsMu.Unlock()
	}()
	running.items, running.cached, running.err = s.sunnyLoadSMSProviderOptionsOnce(ctx, provider, kind, parent, cfg, refresh)
	return running.items, running.cached, running.err
}

func (s *Server) sunnyLoadSMSProviderOptionsOnce(ctx context.Context, provider, kind, parent string, cfg map[string]any, refresh bool) ([]map[string]any, bool, error) {
	// FireFox countries come from a lightweight provider metadata endpoint.
	// Always refresh them so old getItem-derived subsets cannot remain cached.
	if !refresh && !(provider == "firefox" && kind == "country") {
		if items := s.sunnyCachedSMSProviderOptions(provider, kind, parent); len(items) > 0 {
			return items, true, nil
		}
	}
	items, err := s.sunnyFetchSMSProviderOptions(ctx, provider, kind, parent, cfg)
	if err != nil {
		if cached := s.sunnyCachedSMSProviderOptions(provider, kind, parent); len(cached) > 0 {
			return cached, true, err
		}
		return nil, false, err
	}
	s.sunnySaveSMSProviderOptions(provider, kind, parent, items)
	return items, false, nil
}

func (s *Server) sunnyCachedSMSProviderOptions(provider, kind, parent string) []map[string]any {
	var rows []SunnySMSProviderOption
	q := s.db.Where("provider = ? AND kind = ?", provider, kind)
	if kind == "service" && parent != "" {
		q = q.Where("parent_value = ?", parent)
	}
	q.Order("label asc").Find(&rows)
	items := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		items = append(items, map[string]any{"value": row.Value, "label": row.Label, "provider": row.Provider, "kind": row.Kind, "parent_value": row.ParentValue, "extra": jsonMap(row.ExtraJSON)})
	}
	return items
}

func (s *Server) sunnySaveSMSProviderOptions(provider, kind, parent string, items []map[string]any) {
	_ = s.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("provider = ? AND kind = ? AND parent_value = ?", provider, kind, parent).Delete(&SunnySMSProviderOption{}).Error; err != nil {
			return err
		}
		for _, item := range items {
			value := strings.TrimSpace(text(item["value"]))
			if value == "" {
				continue
			}
			row := SunnySMSProviderOption{
				Provider: provider, Kind: kind, ParentValue: parent, Value: value,
				Label: strings.TrimSpace(fallback(text(item["label"]), value)), ExtraJSON: dumpJSON(item["extra"]),
			}
			if err := tx.Create(&row).Error; err != nil {
				return err
			}
		}
		return nil
	})
}

func (s *Server) sunnyFetchSMSProviderOptions(ctx context.Context, provider, kind, parent string, cfg map[string]any) ([]map[string]any, error) {
	switch provider {
	case "smsbower":
		return fetchSMSBowerOptions(ctx, kind, parent, cfg)
	case "smspool":
		return fetchSMSPoolOptions(ctx, kind, parent, cfg)
	case "firefox":
		return fetchFireFoxOptions(ctx, kind, parent, cfg)
	default:
		return nil, fmt.Errorf("invalid sms provider")
	}
}

func (s *Server) sunnyWarmSMSProviderOptions() {
	time.Sleep(800 * time.Millisecond)
	cfg := s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig())
	providers := []struct {
		name           string
		enabledKey     string
		credentialKeys []string
		countryKey     string
	}{
		{name: "smsbower", enabledKey: "smsbower_enabled", credentialKeys: []string{"smsbower_api_key"}, countryKey: "smsbower_default_country"},
		{name: "smspool", enabledKey: "smspool_enabled", credentialKeys: []string{"smspool_api_key"}, countryKey: "smspool_default_country"},
		{name: "firefox", enabledKey: "firefox_enabled", countryKey: "firefox_default_country"},
	}
	for _, p := range providers {
		ready := boolValue(cfg[p.enabledKey], false)
		if p.name == "firefox" {
			ready = ready && fireFoxAPIToken(cfg) != ""
		} else {
			for _, key := range p.credentialKeys {
				ready = ready && strings.TrimSpace(text(cfg[key])) != ""
			}
		}
		if !ready {
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
		_, _, _ = s.sunnyLoadSMSProviderOptions(ctx, p.name, "country", "", cfg, false)
		parent := strings.TrimSpace(text(cfg[p.countryKey]))
		if parent != "" {
			_, _, _ = s.sunnyLoadSMSProviderOptions(ctx, p.name, "service", parent, cfg, false)
		}
		cancel()
	}
}

func fetchSMSBowerOptions(ctx context.Context, kind, parent string, cfg map[string]any) ([]map[string]any, error) {
	apiKey := strings.TrimSpace(text(cfg["smsbower_api_key"]))
	if apiKey == "" {
		return nil, fmt.Errorf("SMSBower API Key is required")
	}
	baseURL := strings.TrimSpace(text(cfg["smsbower_base_url"]))
	if baseURL == "" {
		baseURL = "https://smsbower.page/stubs/handler_api.php"
	}
	params := url.Values{}
	params.Set("api_key", apiKey)
	if kind == "country" {
		params.Set("action", "getCountries")
	} else {
		params.Set("action", "getServicesList")
		params.Set("lang", "en")
		if parent != "" {
			params.Set("country", parent)
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"?"+params.Encode(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("SMSBower HTTP %d: %s", resp.StatusCode, string(b)[:min(len(b), 500)])
	}
	var data any
	if err := json.Unmarshal(b, &data); err != nil {
		return nil, err
	}
	if kind == "country" {
		return normalizeSMSProviderOptions(data, []string{"id", "country", "ID", "code"}, []string{"chn", "eng", "name", "label"}, "country"), nil
	}
	return normalizeSMSProviderOptions(data, []string{"code", "id", "service", "ID"}, []string{"name", "label", "title"}, "service"), nil
}

func fetchSMSPoolOptions(ctx context.Context, kind, parent string, cfg map[string]any) ([]map[string]any, error) {
	apiKey := strings.TrimSpace(text(cfg["smspool_api_key"]))
	baseURL := strings.TrimRight(strings.TrimSpace(text(cfg["smspool_base_url"])), "/")
	if baseURL == "" {
		baseURL = "https://api.smspool.net"
	}
	path := "/country/retrieve_all"
	if kind == "service" {
		path = "/service/retrieve_all"
		if parent != "" {
			path += "?country=" + url.QueryEscape(parent)
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+path, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	if apiKey != "" {
		req.Header.Set("Authorization", "Bearer "+apiKey)
	}
	resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("SMSPool HTTP %d: %s", resp.StatusCode, string(b)[:min(len(b), 500)])
	}
	var data any
	if err := json.Unmarshal(b, &data); err != nil {
		return nil, err
	}
	if kind == "country" {
		return normalizeSMSProviderOptions(data, []string{"ID", "id", "country_id", "short_name"}, []string{"name", "short_name", "region"}, "country"), nil
	}
	return normalizeSMSProviderOptions(data, []string{"ID", "id", "code", "name"}, []string{"name", "code"}, "service"), nil
}

func fetchFireFoxOptions(ctx context.Context, kind, parent string, cfg map[string]any) ([]map[string]any, error) {
	baseURL := strings.TrimSpace(text(cfg["firefox_base_url"]))
	if kind == "country" {
		items, err := fetchFireFoxCountries(ctx, baseURL)
		if err == nil && len(items) > 0 {
			return items, nil
		}
		fallbackItems, fallbackErr := fetchFireFoxCountriesFromItems(ctx, baseURL)
		if fallbackErr != nil {
			return nil, fmt.Errorf("FireFox country list failed: metadata=%v; getItem=%v", err, fallbackErr)
		}
		return fallbackItems, nil
	}
	raw, err := getFireFoxAPI(ctx, baseURL, url.Values{"act": {"getItem"}, "key": {""}})
	if err != nil {
		return nil, err
	}
	var rows []map[string]any
	if err := json.Unmarshal([]byte(raw), &rows); err != nil {
		return nil, fmt.Errorf("FireFox getItem returned invalid JSON: %w", err)
	}
	seen := map[string]bool{}
	items := make([]map[string]any, 0)
	for _, row := range rows {
		countryID := strings.TrimSpace(text(row["Country_ID"]))
		if parent != "" && countryID != parent {
			continue
		}
		serviceID := strings.TrimSpace(text(row["Item_ID"]))
		if serviceID == "" || seen[serviceID] {
			continue
		}
		seen[serviceID] = true
		name := strings.TrimSpace(fallback(text(row["Item_Name"]), serviceID))
		price := strings.TrimSpace(text(row["Item_UPrice"]))
		label := name
		if price != "" {
			label += " · " + price
		}
		items = append(items, map[string]any{
			"value": serviceID,
			"label": label,
			"kind":  "service",
			"extra": row,
		})
	}
	sort.SliceStable(items, func(i, j int) bool {
		return strings.ToLower(text(items[i]["label"])) < strings.ToLower(text(items[j]["label"]))
	})
	return items, nil
}

func fetchFireFoxCountries(ctx context.Context, baseURL string) ([]map[string]any, error) {
	raw, err := postFireFoxForm(ctx, baseURL, "/api/init.ashx", url.Values{"act": {"PagCountry"}})
	if err != nil {
		return nil, err
	}
	var rows []map[string]any
	if err := json.Unmarshal([]byte(raw), &rows); err != nil {
		return nil, fmt.Errorf("FireFox PagCountry returned invalid JSON: %w", err)
	}
	items := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		countryID := strings.TrimSpace(text(row["Country_ID"]))
		if countryID == "" {
			continue
		}
		items = append(items, map[string]any{
			"value": countryID,
			"label": fireFoxCountryLabel(text(row["Country_Title"]), text(row["Country_Area"]), countryID),
			"kind":  "country",
			"extra": row,
		})
	}
	sort.SliceStable(items, func(i, j int) bool { return text(items[i]["value"]) < text(items[j]["value"]) })
	return items, nil
}

func fetchFireFoxCountriesFromItems(ctx context.Context, baseURL string) ([]map[string]any, error) {
	raw, err := getFireFoxAPI(ctx, baseURL, url.Values{"act": {"getItem"}, "key": {""}})
	if err != nil {
		return nil, err
	}
	var rows []map[string]any
	if err := json.Unmarshal([]byte(raw), &rows); err != nil {
		return nil, fmt.Errorf("FireFox getItem returned invalid JSON: %w", err)
	}
	seen := map[string]bool{}
	items := make([]map[string]any, 0)
	for _, row := range rows {
		countryID := strings.TrimSpace(text(row["Country_ID"]))
		if countryID == "" || seen[countryID] {
			continue
		}
		seen[countryID] = true
		items = append(items, map[string]any{
			"value": countryID,
			"label": fireFoxCountryLabel(text(row["Country_Title"]), "", countryID),
			"kind":  "country",
			"extra": row,
		})
	}
	sort.SliceStable(items, func(i, j int) bool { return text(items[i]["value"]) < text(items[j]["value"]) })
	return items, nil
}

func fireFoxCountryLabel(title, area, fallbackValue string) string {
	parts := strings.Split(strings.TrimSpace(title), "/")
	if area == "" && len(parts) > 0 {
		area = strings.TrimPrefix(strings.TrimSpace(parts[0]), "+")
	}
	name := ""
	if len(parts) >= 3 {
		name = strings.TrimSpace(parts[1]) + " / " + strings.TrimSpace(parts[2])
	} else if len(parts) >= 2 {
		name = strings.TrimSpace(parts[1])
	} else {
		name = strings.TrimSpace(title)
	}
	name = fallback(name, fallbackValue)
	if strings.TrimSpace(area) != "" {
		return name + " (+" + strings.TrimPrefix(strings.TrimSpace(area), "+") + ")"
	}
	return name
}

func normalizeSMSProviderOptions(data any, valueKeys []string, labelKeys []string, kind string) []map[string]any {
	var arr []any
	switch v := data.(type) {
	case []any:
		arr = v
	case map[string]any:
		for _, key := range []string{"countries", "services", "data", "items", "result"} {
			if x, ok := v[key].([]any); ok {
				arr = x
				break
			}
		}
		if len(arr) == 0 {
			for key, value := range v {
				if child, ok := value.(map[string]any); ok {
					child["value"] = key
					arr = append(arr, child)
				}
			}
		}
	}
	seen := map[string]bool{}
	items := []map[string]any{}
	for _, raw := range arr {
		m, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		value := firstByKeys(m, valueKeys...)
		label := firstByKeys(m, labelKeys...)
		if value == "" {
			continue
		}
		if label == "" {
			label = value
		}
		if seen[value] {
			continue
		}
		seen[value] = true
		items = append(items, map[string]any{"value": value, "label": label, "kind": kind, "extra": m})
	}
	sort.SliceStable(items, func(i, j int) bool {
		return strings.ToLower(text(items[i]["label"])) < strings.ToLower(text(items[j]["label"]))
	})
	return items
}

func firstByKeys(m map[string]any, keys ...string) string {
	for _, key := range keys {
		if v := strings.TrimSpace(text(m[key])); v != "" {
			return v
		}
	}
	return ""
}

func sunnyPhoneFromBody(body map[string]any) (SunnyPhone, error) {
	if raw := text(body["raw"]); raw != "" {
		return parseSunnyPhoneLine(raw)
	}
	number, smsURL := text(body["number"]), text(body["sms_url"])
	if number == "" || smsURL == "" || !strings.HasPrefix(number, "+") || !strings.HasPrefix(strings.ToLower(smsURL), "http") {
		return SunnyPhone{}, fmt.Errorf("invalid phone format: +phone----https://sms-url")
	}
	statusRaw := strings.ToLower(strings.TrimSpace(text(body["status"])))
	enabled := boolValue(body["enabled"], statusRaw != "disabled")
	status := "available"
	if statusRaw == "disabled" || !enabled {
		status = "disabled"
		enabled = false
	} else {
		enabled = true
	}
	maxSuccess := intValue(body["max_success"], 3)
	if maxSuccess < 1 {
		maxSuccess = 3
	}
	successCount := intValue(body["success_count"], 0)
	if successCount < 0 {
		successCount = 0
	}
	if successCount > maxSuccess {
		successCount = maxSuccess
	}
	return SunnyPhone{Number: number, SmsURL: smsURL, Status: status, Enabled: enabled, SuccessCount: successCount, MaxSuccess: maxSuccess}, nil
}

func parseSunnyPhoneLine(line string) (SunnyPhone, error) {
	raw := strings.TrimSpace(line)
	parts := strings.Split(raw, "----")
	if len(parts) != 2 {
		return SunnyPhone{}, fmt.Errorf("invalid phone format: +phone----https://sms-url")
	}
	number, smsURL := strings.TrimSpace(parts[0]), strings.TrimSpace(parts[1])
	if number == "" || !strings.HasPrefix(number, "+") || smsURL == "" || !strings.HasPrefix(strings.ToLower(smsURL), "http") {
		return SunnyPhone{}, fmt.Errorf("invalid phone format: +phone----https://sms-url")
	}
	return SunnyPhone{Number: number, SmsURL: smsURL, Status: "available", Enabled: true, MaxSuccess: 3}, nil
}

func serializeSunnyPhone(p SunnyPhone) map[string]any {
	displayStatus := "enabled"
	if !p.Enabled || p.Status == "disabled" {
		displayStatus = "disabled"
	}
	return map[string]any{
		"id":             p.ID,
		"number":         p.Number,
		"sms_url":        p.SmsURL,
		"status":         p.Status,
		"display_status": displayStatus,
		"enabled":        p.Enabled,
		"success_count":  p.SuccessCount,
		"max_success":    p.MaxSuccess,
		"cooldown_until": nullableTime(p.CooldownUntil.Valid, p.CooldownUntil.Time),
		"last_used_at":   nullableTime(p.LastUsedAt.Valid, p.LastUsedAt.Time),
		"last_error":     p.LastError,
		"created_at":     formatTime(p.CreatedAt),
		"updated_at":     formatTime(p.UpdatedAt),
	}
}

func (s *Server) sunnyListAccounts(w http.ResponseWriter, r *http.Request) {
	var accounts []SunnyAccount
	s.db.Order("updated_at desc").Find(&accounts)
	emails := []string{}
	for _, a := range accounts {
		emails = append(emails, a.Email)
	}
	sessionPlans := s.sunnySessionPlanTypesByEmail(emails)
	items := []map[string]any{}
	for _, a := range accounts {
		manualPlan := normalizeSunnyPlanType(a.AccountType)
		plan := manualPlan
		if plan == "" || plan == "free" {
			if sessionPlan := sessionPlans[sunnyEmailKey(a.Email)]; sessionPlan != "" {
				plan = sessionPlan
			}
		}
		if plan == "" {
			plan = "free"
		}
		items = append(items, map[string]any{
			"id": a.ID, "mailbox_id": a.MailboxID, "email": a.Email, "group_name": a.GroupName,
			"status": a.Status, "account_type": fallback(a.AccountType, "free"), "plan_type": plan,
			"openai_rt": a.OpenAIRT, "access_token": a.AccessToken, "phone_number": a.PhoneNumber,
			"sub2api_status": a.Sub2APIStatus, "sub2api_id": a.Sub2APIID, "last_error": a.LastError,
			"metadata": jsonMap(a.MetadataJSON), "created_at": formatTime(a.CreatedAt), "updated_at": formatTime(a.UpdatedAt),
		})
	}
	writeJSON(w, 200, map[string]any{"items": items})
}

func (s *Server) sunnyProxyConfig(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 1 && parts[0] == "register-readiness" && r.Method == http.MethodGet {
		writeJSON(w, http.StatusOK, s.sunnyRegisterProxyReadiness())
		return
	}
	if len(parts) == 0 && r.Method == http.MethodGet {
		writeJSON(w, 200, s.sunnyGetConfig(sunnyCfgProxy, defaultProxyConfig()))
		return
	}
	if len(parts) == 0 && r.Method == http.MethodPut {
		body, _ := parseBody(r)
		s.sunnySaveConfig(sunnyCfgProxy, mergeConfig(defaultProxyConfig(), body))
		writeJSON(w, 200, s.sunnyGetConfig(sunnyCfgProxy, defaultProxyConfig()))
		return
	}
	if len(parts) == 1 && parts[0] == "check" && r.Method == http.MethodPost {
		body, _ := parseBody(r)
		proxy := fallback(text(body["proxy"]), text(s.sunnyGetConfig(sunnyCfgProxy, defaultProxyConfig())["local_proxy"]))
		result := map[string]any{"proxy": proxy, "ok": false}
		client := &http.Client{Timeout: 15 * time.Second}
		if proxy != "" {
			u, err := url.Parse(proxy)
			if err == nil {
				client.Transport = &http.Transport{Proxy: http.ProxyURL(u)}
			}
		}
		resp, err := client.Get("https://chatgpt.com/")
		if err != nil {
			result["error"] = err.Error()
		} else {
			result["ok"] = resp.StatusCode < 500
			result["status"] = resp.StatusCode
			_ = resp.Body.Close()
		}
		writeJSON(w, 200, result)
		return
	}
	if len(parts) >= 1 && parts[0] == "pool" {
		s.sunnyProxyPool(w, r, parts[1:])
		return
	}
	writeError(w, 404, "not found")
}

func (s *Server) sunnyProxyPool(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 0 && r.Method == http.MethodGet {
		page, _ := strconv.Atoi(r.URL.Query().Get("page"))
		pageSize, _ := strconv.Atoi(r.URL.Query().Get("page_size"))
		if page <= 0 {
			page = 1
		}
		if pageSize <= 0 {
			pageSize = 10
		}
		if pageSize > 100 {
			pageSize = 100
		}
		query := strings.TrimSpace(r.URL.Query().Get("q"))
		status := normalizeSunnyProxyStatus(r.URL.Query().Get("status"))
		country := strings.TrimSpace(r.URL.Query().Get("country"))
		purpose := normalizeSunnyProxyPurpose(r.URL.Query().Get("purpose"))
		db := s.db.Model(&SunnyProxy{})
		if query != "" {
			like := "%" + query + "%"
			db = db.Where("address LIKE ? OR country LIKE ?", like, like)
		}
		if country != "" {
			db = db.Where("country = ?", country)
		}
		if purpose != "" {
			db = db.Where("(',' || replace(lower(coalesce(purpose_tags, '')), ' ', '') || ',') LIKE ?", "%,"+purpose+",%")
		}
		switch status {
		case "enabled":
			db = db.Where("status = ? AND enabled = ?", "enabled", true)
		case "disabled":
			db = db.Where("status = ?", "disabled")
		case "invalid":
			db = db.Where("status = ? OR (last_check_ok = ? AND last_checked_at IS NOT NULL)", "invalid", false)
		}
		if strings.EqualFold(strings.TrimSpace(r.URL.Query().Get("selection")), "all") {
			var ids []uint
			db.Order("id desc").Pluck("id", &ids)
			writeJSON(w, 200, map[string]any{"ids": ids, "total": len(ids)})
			return
		}
		var total int64
		db.Count(&total)
		var proxies []SunnyProxy
		db.Order(sunnySortClause(r.URL.Query().Get("sort_by"), r.URL.Query().Get("sort_order"), map[string]string{"last_checked_at": "last_checked_at", "updated_at": "updated_at", "created_at": "created_at"}, "updated_at desc")).Limit(pageSize).Offset((page - 1) * pageSize).Find(&proxies)
		var proxyStats struct {
			Total    int64 `gorm:"column:total"`
			Enabled  int64 `gorm:"column:enabled"`
			Disabled int64 `gorm:"column:disabled"`
			Invalid  int64 `gorm:"column:invalid"`
		}
		s.db.Model(&SunnyProxy{}).Select(`
			COUNT(*) AS total,
			COALESCE(SUM(CASE WHEN status = 'enabled' AND enabled = true THEN 1 ELSE 0 END), 0) AS enabled,
			COALESCE(SUM(CASE WHEN status = 'disabled' THEN 1 ELSE 0 END), 0) AS disabled,
			COALESCE(SUM(CASE WHEN status = 'invalid' OR (last_check_ok = false AND last_checked_at IS NOT NULL) THEN 1 ELSE 0 END), 0) AS invalid`).Scan(&proxyStats)
		var countries []string
		s.db.Model(&SunnyProxy{}).Where("country <> ''").Distinct().Order("country asc").Pluck("country", &countries)
		items := make([]map[string]any, 0, len(proxies))
		for _, p := range proxies {
			items = append(items, sunnyProxyJSON(p))
		}
		writeJSON(w, 200, map[string]any{
			"items": items, "total": total, "page": page, "page_size": pageSize, "countries": countries,
			"stats": map[string]any{"total": proxyStats.Total, "enabled": proxyStats.Enabled, "disabled": proxyStats.Disabled, "invalid": proxyStats.Invalid},
		})
		return
	}
	if len(parts) == 0 && r.Method == http.MethodPost {
		body, _ := parseBody(r)
		addresses := []string{}
		if arr, ok := body["addresses"].([]any); ok {
			for _, raw := range arr {
				if v := normalizeSunnyProxyAddress(text(raw)); v != "" {
					addresses = append(addresses, v)
				}
			}
		}
		if lines := strings.TrimSpace(text(body["lines"])); lines != "" {
			for _, line := range strings.Split(lines, "\n") {
				if v := normalizeSunnyProxyAddress(line); v != "" {
					addresses = append(addresses, v)
				}
			}
		}
		if address := normalizeSunnyProxyAddress(text(body["address"])); address != "" {
			addresses = append(addresses, address)
		}
		if len(addresses) == 0 {
			writeError(w, 400, "proxy address is required")
			return
		}
		purposeTags, hasPurposeTags := body["purpose_tags"]
		normalizedPurposeTags := normalizeSunnyProxyPurposes(purposeTags)
		if !hasPurposeTags {
			normalizedPurposeTags = []string{sunnyProxyPurposeRegister}
		}
		country, countryErr := normalizeSunnyProxyCountry(text(body["country"]))
		if countryErr != nil {
			writeError(w, http.StatusBadRequest, countryErr.Error())
			return
		}
		enabled := true
		if v, ok := body["enabled"]; ok {
			enabled = asBool(v)
		}
		created := []map[string]any{}
		for _, address := range addresses {
			p := SunnyProxy{
				Address:     address,
				Country:     country,
				PurposeTags: strings.Join(normalizedPurposeTags, ","),
				Status:      fallback(normalizeSunnyProxyStatus(text(body["status"])), "enabled"),
				Enabled:     enabled,
			}
			if !p.Enabled {
				p.Status = "disabled"
			}
			if p.Status == "invalid" {
				p.Enabled = false
				p.LastCheckOK = false
			}
			if p.Enabled {
				applySunnyProxyCheck(&p, checkSunnyProxy(address))
			}
			if err := s.db.Create(&p).Error; err != nil {
				writeError(w, 400, err.Error())
				return
			}
			created = append(created, sunnyProxyJSON(p))
		}
		if len(created) == 1 {
			writeJSON(w, 200, created[0])
		} else {
			writeJSON(w, 200, map[string]any{"items": created, "created": len(created)})
		}
		return
	}
	if len(parts) == 1 && parts[0] == "check" && r.Method == http.MethodPost {
		body, _ := parseBody(r)
		var proxies []SunnyProxy
		if rawIDs, ok := body["ids"].([]any); ok && len(rawIDs) > 0 {
			ids := make([]uint, 0, len(rawIDs))
			for _, v := range rawIDs {
				if id := uint(toInt(v)); id > 0 {
					ids = append(ids, id)
				}
			}
			s.db.Where("id IN ?", ids).Find(&proxies)
		} else {
			s.db.Where("enabled = ?", true).Order("updated_at desc").Limit(200).Find(&proxies)
		}
		okCount := 0
		for i := range proxies {
			result := checkSunnyProxy(proxies[i].Address)
			applySunnyProxyCheck(&proxies[i], result)
			if proxies[i].LastCheckOK {
				okCount++
			}
			s.db.Save(&proxies[i])
		}
		writeJSON(w, 200, map[string]any{"checked": len(proxies), "available": okCount})
		return
	}
	if len(parts) >= 1 {
		id64, err := strconv.ParseUint(parts[0], 10, 64)
		if err != nil || id64 == 0 {
			writeError(w, 400, "invalid proxy id")
			return
		}
		var p SunnyProxy
		if err := s.db.First(&p, uint(id64)).Error; err != nil {
			writeError(w, 404, "proxy not found")
			return
		}
		if len(parts) == 1 && r.Method == http.MethodPut {
			body, _ := parseBody(r)
			if v := normalizeSunnyProxyAddress(text(body["address"])); v != "" {
				p.Address = v
			}
			if rawCountry := strings.TrimSpace(text(body["country"])); rawCountry != "" {
				country, countryErr := normalizeSunnyProxyCountry(rawCountry)
				if countryErr != nil {
					writeError(w, http.StatusBadRequest, countryErr.Error())
					return
				}
				p.Country = country
			}
			if _, ok := body["purpose_tags"]; ok {
				p.PurposeTags = strings.Join(normalizeSunnyProxyPurposes(body["purpose_tags"]), ",")
			}
			if containsString(normalizeSunnyProxyPurposes(p.PurposeTags), sunnyProxyPurposePayment) {
				if _, countryErr := normalizeSunnyProxyCountry(p.Country); countryErr != nil {
					writeError(w, http.StatusBadRequest, "支付探测代理必须配置有效国家代码")
					return
				}
			}
			if _, ok := body["enabled"]; ok {
				p.Enabled = asBool(body["enabled"])
			}
			if v := normalizeSunnyProxyStatus(text(body["status"])); v != "" {
				p.Status = v
				if v == "disabled" {
					p.Enabled = false
				}
				if v == "enabled" {
					p.Enabled = true
				}
				if v == "invalid" {
					p.Enabled = false
					p.LastCheckOK = false
				}
			}
			if !p.Enabled && p.Status != "invalid" {
				p.Status = "disabled"
			} else if p.Status == "disabled" {
				p.Status = "enabled"
			}
			if err := s.db.Save(&p).Error; err != nil {
				writeError(w, 400, err.Error())
				return
			}
			writeJSON(w, 200, sunnyProxyJSON(p))
			return
		}
		if len(parts) == 1 && r.Method == http.MethodDelete {
			s.db.Delete(&p)
			writeJSON(w, 200, map[string]any{"ok": true})
			return
		}
		if len(parts) == 2 && parts[1] == "check" && r.Method == http.MethodPost {
			result := checkSunnyProxy(p.Address)
			applySunnyProxyCheck(&p, result)
			s.db.Save(&p)
			writeJSON(w, 200, sunnyProxyJSON(p))
			return
		}
	}
	writeError(w, 404, "not found")
}

func normalizeSunnyProxyStatus(status string) string {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "启用", "可用", "enabled", "available", "enable", "on", "ok", "valid":
		return "enabled"
	case "停用", "disabled", "disable", "off":
		return "disabled"
	case "失效", "invalid", "failed", "fail":
		return "invalid"
	default:
		return ""
	}
}

const (
	sunnyProxyPurposeRegister = "register"
	sunnyProxyPurposeCommerce = "commerce"
	sunnyProxyPurposePayment  = "payment_probe"
)

func normalizeSunnyProxyPurpose(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "register", "registration", "login", "注册", "登录":
		return sunnyProxyPurposeRegister
	case "commerce", "trial", "checkout", "account_check", "账户检测", "商业检测":
		return sunnyProxyPurposeCommerce
	case "payment_probe", "payment", "payment_check", "支付探测", "支付检测":
		return sunnyProxyPurposePayment
	default:
		return ""
	}
}

func normalizeSunnyProxyPurposes(value any) []string {
	values := []string{}
	switch raw := value.(type) {
	case []any:
		for _, item := range raw {
			if purpose := normalizeSunnyProxyPurpose(text(item)); purpose != "" && !containsString(values, purpose) {
				values = append(values, purpose)
			}
		}
	case []string:
		for _, item := range raw {
			if purpose := normalizeSunnyProxyPurpose(item); purpose != "" && !containsString(values, purpose) {
				values = append(values, purpose)
			}
		}
	default:
		for _, item := range strings.FieldsFunc(text(value), func(r rune) bool { return r == ',' || r == ';' || r == '|' }) {
			if purpose := normalizeSunnyProxyPurpose(item); purpose != "" && !containsString(values, purpose) {
				values = append(values, purpose)
			}
		}
	}
	return values
}

func containsString(values []string, target string) bool {
	for _, value := range values {
		if value == target {
			return true
		}
	}
	return false
}

func normalizeSunnyProxyAddress(raw string) string {
	value := strings.TrimSpace(raw)
	if value == "" {
		return ""
	}
	if strings.Contains(value, "://") {
		if u, err := url.Parse(value); err == nil && u.Scheme != "" && u.Host != "" {
			return u.String()
		}
		return value
	}
	if strings.Contains(value, "@") {
		chunks := strings.SplitN(value, "@", 2)
		leftParts := strings.Split(chunks[0], ":")
		rightParts := strings.Split(chunks[1], ":")
		if len(leftParts) >= 2 && len(rightParts) >= 2 && looksLikeProxyHost(leftParts[0]) {
			if _, err := strconv.Atoi(leftParts[1]); err == nil {
				host := leftParts[0]
				port := leftParts[1]
				user := rightParts[0]
				pass := strings.Join(rightParts[1:], ":")
				return (&url.URL{Scheme: "http", User: url.UserPassword(user, pass), Host: net.JoinHostPort(host, port)}).String()
			}
		}
		return "http://" + value
	}
	parts := strings.Split(value, ":")
	if len(parts) >= 4 {
		last := parts[len(parts)-1]
		if _, err := strconv.Atoi(last); err == nil {
			host := parts[len(parts)-2]
			if looksLikeProxyHost(host) {
				user := parts[0]
				pass := strings.Join(parts[1:len(parts)-2], ":")
				return (&url.URL{Scheme: "http", User: url.UserPassword(user, pass), Host: net.JoinHostPort(host, last)}).String()
			}
		}
		if _, err := strconv.Atoi(parts[1]); err == nil && looksLikeProxyHost(parts[0]) {
			host := parts[0]
			port := parts[1]
			user := parts[2]
			pass := strings.Join(parts[3:], ":")
			return (&url.URL{Scheme: "http", User: url.UserPassword(user, pass), Host: net.JoinHostPort(host, port)}).String()
		}
	}
	return "http://" + value
}

func looksLikeProxyHost(value string) bool {
	host := strings.Trim(value, "[]")
	if host == "" {
		return false
	}
	if net.ParseIP(host) != nil {
		return true
	}
	lower := strings.ToLower(host)
	return lower == "localhost" || strings.Contains(host, ".")
}

func sunnyProxyDisplayStatus(p SunnyProxy) string {
	switch normalizeSunnyProxyStatus(p.Status) {
	case "invalid":
		return "失效"
	case "disabled":
		return "停用"
	case "enabled":
		return "启用"
	}
	if p.LastCheckedAt != nil && !p.LastCheckOK {
		return "失效"
	}
	if !p.Enabled {
		return "停用"
	}
	return "启用"
}

func sunnyProxyJSON(p SunnyProxy) map[string]any {
	tags := normalizeSunnyProxyPurposes(p.PurposeTags)
	return map[string]any{
		"id": p.ID, "address": p.Address, "country": p.Country, "purpose_tags": tags, "status": sunnyProxyDisplayStatus(p), "status_key": normalizeSunnyProxyStatus(p.Status),
		"enabled": p.Enabled, "last_check_ok": p.LastCheckOK, "latency_ms": p.LatencyMS, "last_error": p.LastError,
		"last_checked_at": nullableTime(p.LastCheckedAt != nil, pointerTime(p.LastCheckedAt)), "created_at": formatTime(p.CreatedAt), "updated_at": formatTime(p.UpdatedAt),
	}
}

func checkSunnyProxy(proxyAddr string) map[string]any {
	proxyAddr = normalizeSunnyProxyAddress(proxyAddr)
	result := map[string]any{"proxy": proxyAddr, "ok": false, "latency_ms": int64(0), "check_mode": "tcp_connect"}
	if proxyAddr == "" {
		result["error"] = "proxy is empty"
		return result
	}
	u, err := url.Parse(proxyAddr)
	if err != nil || u.Scheme == "" || u.Host == "" {
		result["error"] = "proxy format is invalid"
		return result
	}
	host := u.Hostname()
	port := u.Port()
	if port == "" {
		switch strings.ToLower(u.Scheme) {
		case "socks5", "socks5h":
			port = "1080"
		case "https":
			port = "443"
		default:
			port = "80"
		}
	}
	if host == "" || port == "" {
		result["error"] = "proxy host or port is empty"
		return result
	}
	start := time.Now()
	conn, err := net.DialTimeout("tcp", net.JoinHostPort(host, port), 8*time.Second)
	result["latency_ms"] = time.Since(start).Milliseconds()
	if err != nil {
		result["error"] = err.Error()
		return result
	}
	_ = conn.Close()
	result["ok"] = true
	return result
}

func applySunnyProxyCheck(p *SunnyProxy, result map[string]any) {
	now := time.Now()
	p.LastCheckedAt = &now
	if normalized := normalizeSunnyProxyAddress(text(result["proxy"])); normalized != "" {
		p.Address = normalized
	}
	p.LastCheckOK = asBool(result["ok"])
	p.LatencyMS = int64(toInt(result["latency_ms"]))
	p.LastError = text(result["error"])
	if p.LastCheckOK {
		if normalizeSunnyProxyStatus(p.Status) == "disabled" || !p.Enabled {
			p.Status = "disabled"
			p.Enabled = false
		} else {
			p.Status = "enabled"
			p.Enabled = true
		}
	} else {
		p.Status = "invalid"
		p.Enabled = false
	}
}

func asBool(v any) bool {
	switch x := v.(type) {
	case bool:
		return x
	case string:
		s := strings.ToLower(strings.TrimSpace(x))
		return s == "true" || s == "1" || s == "yes" || s == "on" || s == "启用" || s == "可用"
	case float64:
		return x != 0
	case int:
		return x != 0
	case int64:
		return x != 0
	default:
		return false
	}
}

func toInt(v any) int {
	switch x := v.(type) {
	case int:
		return x
	case int64:
		return int(x)
	case float64:
		return int(x)
	case json.Number:
		i, _ := x.Int64()
		return int(i)
	case string:
		i, _ := strconv.Atoi(strings.TrimSpace(x))
		return i
	default:
		return 0
	}
}

func defaultProxyConfig() map[string]any {
	return map[string]any{
		"proxy_enabled": true, "local_proxy": "http://127.0.0.1:7897", "register_proxy": "", "provider_configs": []any{}, "precheck": true, "sid_mode": "random",
		"browser_traffic_optimization": map[string]any{
			"enabled": true, "block_heavy_resources": true, "static_cache_enabled": true,
			"cache_ttl_hours": 168, "cache_max_mib": 256, "cache_object_max_mib": 8,
		},
	}
}

func (s *Server) sunnySub2APIConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodGet {
		writeJSON(w, 200, s.sunnyGetConfig(sunnyCfgSub2API, defaultSub2APIConfig()))
		return
	}
	if r.Method == http.MethodPut {
		body, _ := parseBody(r)
		s.sunnySaveConfig(sunnyCfgSub2API, mergeConfig(defaultSub2APIConfig(), body))
		writeJSON(w, 200, s.sunnyGetConfig(sunnyCfgSub2API, defaultSub2APIConfig()))
		return
	}
	writeError(w, 404, "not found")
}

func defaultSub2APIConfig() map[string]any {
	return map[string]any{
		"enabled": true, "base_url": "", "admin_token": "", "name_prefix": "", "codex_image_bridge": false,
		"group_ids": []any{}, "proxy_id": 0, "concurrency": 3, "load_factor": 0, "priority": 50, "model_whitelist": []any{},
		"notes_include_sk": false, "notes_include_ls": false, "notes_include_custom": false, "notes_custom_text": "",
	}
}

func firstNonNil(values ...any) any {
	for _, value := range values {
		if value != nil {
			return value
		}
	}
	return []any{}
}

func (s *Server) sunnySub2API(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 1 && parts[0] == "options" && (r.Method == http.MethodGet || r.Method == http.MethodPost) {
		cfg := s.sunnyGetConfig(sunnyCfgSub2API, defaultSub2APIConfig())
		if r.Method == http.MethodPost {
			body, _ := parseBody(r)
			cfg = mergeConfig(cfg, body)
		}
		baseURL, token := strings.TrimRight(text(cfg["base_url"]), "/"), text(cfg["admin_token"])
		if baseURL == "" || token == "" {
			writeError(w, 400, "Please fill sub2api Base URL and Admin Token")
			return
		}
		groups, groupErr := callSub2API(r.Context(), baseURL, "/api/v1/admin/groups/all?platform=openai", token, "x-api-key", nil)
		proxies, proxyErr := callSub2API(r.Context(), baseURL, "/api/v1/admin/proxies/all", token, "x-api-key", nil)
		if groupErr != nil || proxyErr != nil {
			writeError(w, 502, fmt.Sprintf("Sub2API options failed: groups=%v proxies=%v", groupErr, proxyErr))
			return
		}
		writeJSON(w, 200, map[string]any{"groups": firstNonNil(groups["data"], groups["items"], groups["groups"]), "proxies": firstNonNil(proxies["data"], proxies["items"], proxies["proxies"])})
		return
	}
	if len(parts) == 1 && parts[0] == "groups" && (r.Method == http.MethodGet || r.Method == http.MethodPost) {
		cfg := s.sunnyGetConfig(sunnyCfgSub2API, defaultSub2APIConfig())
		baseURL, token := strings.TrimRight(text(cfg["base_url"]), "/"), text(cfg["admin_token"])
		if r.Method == http.MethodPost {
			body, err := parseBody(r)
			if err != nil {
				writeError(w, http.StatusBadRequest, "Invalid sub2api request")
				return
			}
			if value := strings.TrimRight(text(body["base_url"]), "/"); value != "" {
				baseURL = value
			}
			if value := text(body["admin_token"]); value != "" {
				token = value
			}
		}
		if baseURL == "" || token == "" {
			writeError(w, 400, "Please fill sub2api Base URL and Admin Token")
			return
		}
		resp, err := callSub2API(r.Context(), baseURL, "/api/v1/admin/groups/all?platform=openai", token, "x-api-key", nil)
		if err != nil {
			writeError(w, 502, err.Error())
			return
		}
		writeJSON(w, 200, resp)
		return
	}
	if len(parts) == 1 && parts[0] == "import" && r.Method == http.MethodPost {
		s.sunnySub2APIImport(w, r)
		return
	}
	writeError(w, 404, "not found")
}

func (s *Server) sunnySub2APIImport(w http.ResponseWriter, r *http.Request) {
	body, _ := parseBody(r)
	cfg := mergeConfig(s.sunnyGetConfig(sunnyCfgSub2API, defaultSub2APIConfig()), body)
	baseURL, token := strings.TrimRight(text(cfg["base_url"]), "/"), text(cfg["admin_token"])
	if baseURL == "" || token == "" {
		writeError(w, 400, "请先配置 Sub2API Base URL 和 Admin Token")
		return
	}
	ids := uintSlice(body["account_ids"])
	sessionIDs := uintSlice(body["session_ids"])
	if len(ids) == 0 && len(sessionIDs) == 0 {
		writeError(w, 400, "请选择需要导入 Sub2API 的账户")
		return
	}
	var sessions []SunnySession
	q := s.db.Model(&SunnySession{})
	if len(ids) > 0 && len(sessionIDs) > 0 {
		q = q.Where("account_id IN ? OR id IN ?", ids, sessionIDs)
	} else if len(ids) > 0 {
		q = q.Where("account_id IN ?", ids)
	} else {
		q = q.Where("id IN ?", sessionIDs)
	}
	q.Find(&sessions)
	if len(sessions) == 0 {
		writeError(w, 400, "未找到需要导入的账户 Session")
		return
	}
	accountRows, mailboxRows := s.sunnySessionSidecars(sessions)
	accounts := []any{}
	validSessions := []SunnySession{}
	skipped := []map[string]any{}
	for _, sess := range sessions {
		key := sunnyEmailKey(sess.Email)
		account := accountRows[key]
		sess.AccessToken = sunnyPreferredAccessToken(sess.AccessToken, sunnyAccessTokenFromSessionJSON(sess.SessionJSON), account.AccessToken)
		sess.RefreshToken = firstText(sess.RefreshToken, account.OpenAIRT)
		sess.RawMailboxLine = sunnySessionSecretKey(sess, mailboxRows[key])
		if strings.TrimSpace(sess.AccessToken) == "" || strings.TrimSpace(sess.RefreshToken) == "" {
			skipped = append(skipped, map[string]any{"email": sess.Email, "reason": "missing access token or refresh token"})
			continue
		}
		accounts = append(accounts, buildSunnySub2AccountPayload(sess, cfg, mailboxRows[key]))
		validSessions = append(validSessions, sess)
	}
	if len(accounts) == 0 {
		writeJSON(w, 200, map[string]any{"selected": len(sessions), "uploaded": 0, "failed": 0, "skipped": skipped})
		return
	}
	idempotencyKey := fmt.Sprintf("sunny-sub2-%d", time.Now().UnixNano())
	resp, err := callSub2APIWithHeaders(r.Context(), baseURL, "/api/v1/admin/accounts/batch", token, "x-api-key", map[string]any{"accounts": accounts}, map[string]string{"Idempotency-Key": idempotencyKey}, true)
	if err != nil {
		writeError(w, 502, err.Error())
		return
	}
	resultData := resp
	if data, ok := resp["data"].(map[string]any); ok {
		resultData = data
	}
	remoteSuccess := intValue(firstNonNil(resultData["success"], resultData["succeeded"], resultData["created"]), -1)
	remoteFailed := intValue(resultData["failed"], -1)
	updated := 0
	confirmedEmails := map[string]string{}
	if remoteSuccess == len(validSessions) && remoteFailed == 0 {
		for _, sess := range validSessions {
			confirmedEmails[sunnyEmailKey(sess.Email)] = ""
		}
	} else {
		if results, ok := resultData["results"].([]any); ok {
			for _, rawResult := range results {
				item, ok := rawResult.(map[string]any)
				if !ok || !sunnySub2ResultSucceeded(item) {
					continue
				}
				email, remoteID := sunnySub2ResultIdentity(item)
				for _, sess := range validSessions {
					if email == sunnyEmailKey(sess.Email) || email == sunnyEmailKey(text(cfg["name_prefix"])+sess.Email) {
						confirmedEmails[sunnyEmailKey(sess.Email)] = remoteID
						break
					}
				}
			}
		}
	}
	for _, sess := range validSessions {
		remoteID, confirmed := confirmedEmails[sunnyEmailKey(sess.Email)]
		if !confirmed {
			continue
		}
		updates := map[string]any{"status": "reverse_proxied", "sub2api_status": "imported", "last_error": ""}
		if remoteID != "" {
			updates["sub2api_id"] = remoteID
		}
		s.db.Model(&SunnyAccount{}).Where("email = ?", sess.Email).Updates(updates)
		s.db.Model(&SunnyMailbox{}).Where("email = ?", sess.Email).Updates(map[string]any{"status": "已反代", "last_error": ""})
		updated++
	}
	if remoteFailed < 0 {
		remoteFailed = 0
	}
	unconfirmed := len(validSessions) - updated - remoteFailed
	if unconfirmed < 0 {
		unconfirmed = 0
	}
	writeJSON(w, 200, map[string]any{"selected": len(sessions), "uploaded": len(validSessions), "confirmed": updated, "failed": remoteFailed, "unconfirmed": unconfirmed, "skipped": skipped, "response": resp, "idempotency_key": idempotencyKey})
}

func sunnySub2ResultSucceeded(item map[string]any) bool {
	if value, ok := item["success"].(bool); ok {
		return value
	}
	status := strings.ToLower(strings.TrimSpace(firstText(item["status"], item["state"])))
	return status == "success" || status == "succeeded" || status == "created" || status == "imported"
}

func sunnySub2ResultIdentity(item map[string]any) (string, string) {
	nested, _ := item["account"].(map[string]any)
	email := sunnyEmailKey(firstText(
		item["email"], item["account_email"], item["name"],
		nested["email"], nested["account_email"], nested["name"],
	))
	remoteID := firstText(
		item["id"], item["account_id"], item["remote_id"],
		nested["id"], nested["account_id"], nested["remote_id"],
	)
	return email, remoteID
}

func buildSunnySub2AccountPayload(sess SunnySession, cfg map[string]any, mailboxes ...SunnyMailbox) map[string]any {
	claims := decodeJWTPayload(sess.AccessToken)
	auth, _ := claims["https://api.openai.com/auth"].(map[string]any)
	sessionData := jsonMap(sess.SessionJSON)
	groupIDs := []int64{}
	for _, raw := range stringSlice(cfg["group_ids"]) {
		if n, err := strconv.ParseInt(raw, 10, 64); err == nil && n > 0 {
			groupIDs = append(groupIDs, n)
		}
	}
	if len(groupIDs) == 0 {
		for _, raw := range uintSlice(cfg["group_ids"]) {
			groupIDs = append(groupIDs, int64(raw))
		}
	}
	extra := map[string]any{
		"import_source": "sunnyregister", "email": sess.Email, "privacy_mode": "training_off",
		"openai_long_context_billing_enabled": false, "openai_oauth_responses_websockets_v2_enabled": false,
		"openai_oauth_responses_websockets_v2_mode": "off",
	}
	if boolValue(cfg["codex_image_bridge"], false) {
		extra["codex_image_generation_bridge"] = true
	}
	credentials := map[string]any{
		"access_token": sess.AccessToken, "refresh_token": fallback(sess.RefreshToken, text(auth["refresh_token"])), "id_token": sess.IDToken,
		"email": sess.Email, "client_id": fallback(text(auth["client_id"]), "app_EMoamEEZ73f0CkXaXp7hrann"),
		"chatgpt_account_id": firstText(auth["chatgpt_account_id"], auth["account_id"]), "chatgpt_user_id": firstText(auth["user_id"], claims["sub"]),
		"organization_id": firstText(auth["organization_id"], auth["poid"]), "plan_type": firstText(auth["chatgpt_plan_type"], auth["plan_type"], auth["plan"]),
		"expires_at": intValue(claims["exp"], 0),
	}
	if modelMapping, ok := sessionData["model_mapping"].(map[string]any); ok && len(modelMapping) > 0 {
		credentials["model_mapping"] = modelMapping
	} else {
		credentials["model_mapping"] = sunnyDefaultSub2ModelMapping()
	}
	if subscriptionExpiresAt := intValue(firstText(sessionData["subscription_expires_at"], auth["subscription_expires_at"]), 0); subscriptionExpiresAt > 0 {
		credentials["subscription_expires_at"] = subscriptionExpiresAt
	} else {
		credentials["subscription_expires_at"] = 0
	}
	if models := stringSlice(cfg["model_whitelist"]); len(models) > 0 {
		mapping := map[string]any{}
		for _, model := range models {
			if strings.TrimSpace(model) != "" {
				mapping[model] = model
			}
		}
		credentials["model_mapping"] = mapping
	}
	mailbox := SunnyMailbox{}
	if len(mailboxes) > 0 {
		mailbox = mailboxes[0]
	}
	payload := map[string]any{
		"name": fallback(text(cfg["name_prefix"])+sess.Email, sess.Email), "notes": sunnySub2NotesWithConfig(mailbox, sess.RawMailboxLine, cfg), "platform": "openai", "type": "oauth",
		"credentials": credentials,
		"extra":       extra, "group_ids": groupIDs, "concurrency": intValue(cfg["concurrency"], 3), "priority": intValue(cfg["priority"], 50),
		"rate_multiplier": 1, "auto_pause_on_expired": true,
	}
	if proxyID := intValue(cfg["proxy_id"], 0); proxyID > 0 {
		payload["proxy_id"] = proxyID
	}
	if loadFactor := intValue(cfg["load_factor"], 0); loadFactor > 0 {
		payload["load_factor"] = loadFactor
	}
	return payload
}

func sunnySessionSecretKey(sess SunnySession, mailbox SunnyMailbox) string {
	if mailbox.ID != 0 {
		if value := strings.TrimSpace(sunnyMailboxCredentialLine(mailbox)); value != "" {
			return value
		}
	}
	return sunnyCanonicalMailboxCredential(sess.RawMailboxLine, "", "")
}

func sunnyDefaultSub2ModelMapping() map[string]any {
	models := []string{"codex-auto-review", "gpt-5.4", "gpt-5.4-mini", "gpt-5.5", "gpt-5.6", "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-image-1.5", "gpt-image-2"}
	mapping := make(map[string]any, len(models))
	for _, model := range models {
		mapping[model] = model
	}
	return mapping
}

func normalizeSunnyDisplayStatus(status string) string {
	raw := strings.TrimSpace(status)
	switch strings.ToLower(raw) {
	case "registered", "success", "succeeded":
		return "已注册"
	case "phone_bound", "phone-bound", "bound":
		return "已接码"
	case "reverse_proxied", "reverse-proxied", "proxied", "imported":
		return "已反代"
	case "failed", "error":
		return "失败"
	case "pending", "":
		return "已注册"
	default:
		return raw
	}
}

func sunnyPhoneBindingCompleted(phoneNumber string, statuses ...string) bool {
	if strings.TrimSpace(phoneNumber) != "" {
		return true
	}
	for _, status := range statuses {
		raw := strings.TrimSpace(status)
		switch strings.ToLower(raw) {
		case "phone_bound", "phone-bound", "bound":
			return true
		}
		if raw == "已接码" || raw == "PLUS试用中" {
			return true
		}
	}
	return false
}

func (s *Server) serializeSunnySession(sess SunnySession, accounts map[string]SunnyAccount, mailboxes map[string]SunnyMailbox) map[string]any {
	key := sunnyEmailKey(sess.Email)
	acc := accounts[key]
	mb := mailboxes[key]
	displayEmail := strings.TrimSpace(sess.Email)
	if mbEmail := strings.TrimSpace(mb.Email); mbEmail != "" {
		displayEmail = mbEmail
	}
	statusSource := acc.Status
	if mb.ID != 0 {
		statusSource = mb.Status
		if strings.TrimSpace(statusSource) == "" {
			statusSource = "unused"
		}
	}
	status := normalizeSunnyDisplayStatus(statusSource)
	sessionPlan := sunnyPlanTypeFromSessionJSON(sess.SessionJSON)
	plan := ""
	if mb.ID != 0 {
		if mailboxPlan := normalizeSunnyPlanType(mb.AccountType); mailboxPlan != "" && mailboxPlan != "free" {
			plan = mailboxPlan
		} else if sessionPlan != "" {
			plan = sessionPlan
		} else if acc.ID != 0 || strings.TrimSpace(mb.OpenAIRT) != "" || sunnyMailboxStatusLooksRegistered(mb.Status) {
			plan = fallback(normalizeSunnyPlanType(mb.AccountType), "free")
		}
	} else {
		plan = normalizeSunnyPlanType(acc.AccountType)
		if plan == "" {
			plan = sessionPlan
		}
		if plan == "" && (sess.AccessToken != "" || sunnyAccessTokenFromSessionJSON(sess.SessionJSON) != "" || acc.ID != 0 || sunnyMailboxStatusLooksRegistered(status)) {
			plan = "free"
		}
	}
	raw := sess.RawMailboxLine
	if raw == "" && mb.Raw != "" {
		raw = mb.Raw
	}
	refreshToken := sess.RefreshToken
	if refreshToken == "" {
		refreshToken = acc.OpenAIRT
	}
	accessToken := sunnyPreferredAccessToken(sess.AccessToken, sunnyAccessTokenFromSessionJSON(sess.SessionJSON), acc.AccessToken)
	expiresAt := sunnyAccessTokenExpiry(accessToken, sess.ExpiresAt)
	trialEligibility := sunnyTrialEligibilityFor(acc.TrialEligibility, mb.TrialEligibility)
	trialCheckedAt := acc.TrialCheckedAt
	if trialCheckedAt == nil {
		trialCheckedAt = mb.TrialCheckedAt
	}
	groupName := ""
	if mb.GroupID != 0 {
		groupName = s.sunnyGroupMap()[mb.GroupID]
	}
	return map[string]any{
		"id": sess.ID, "account_id": sess.AccountID, "mailbox_id": mb.ID, "email": displayEmail,
		"status": status, "plan_type": plan, "group_id": mb.GroupID, "group_name": groupName,
		"phone_bound":           sunnyPhoneBindingCompleted(acc.PhoneNumber, acc.Status, mb.Status),
		"rebind_email":          fallback(strings.TrimSpace(mb.RebindEmail), strings.TrimSpace(acc.RebindEmail)),
		"rebind_mailbox_api":    fallback(strings.TrimSpace(mb.RebindMailboxAPI), strings.TrimSpace(acc.RebindMailboxAPI)),
		"trial_eligibility":     trialEligibility,
		"trial_country_results": sunnyTrialCountryResults(acc.TrialCountryResultsJSON, mb.TrialCountryResultsJSON),
		"access_token":          accessToken, "refresh_token": refreshToken, "id_token": sess.IDToken,
		"session_json": sess.SessionJSON, "storage_state_json": sess.StorageStateJSON,
		"raw_mailbox_line": raw,
		"mailbox_password": mb.Password, "mailbox_client_id": mb.ClientID, "mailbox_refresh_token": mb.RefreshToken,
		"has_chatgpt_password": strings.TrimSpace(mb.ChatGPTPassword) != "", "has_totp_secret": strings.TrimSpace(mb.TOTPSecret) != "", "has_login_secret": sunnyLoginSecretLine(mb) != "",
		"expires_at":      nullableTime(expiresAt.Valid, expiresAt.Time),
		"last_refresh_at": nullableTime(sess.LastRefreshAt.Valid, sess.LastRefreshAt.Time),
		"created_at":      formatTime(sess.CreatedAt), "updated_at": formatTime(sess.UpdatedAt),
		"trial_checked_at": nullableTime(trialCheckedAt != nil, pointerTime(trialCheckedAt)),
	}
}

func sunnyAccessTokenExpiry(accessToken string, stored sql.NullTime) sql.NullTime {
	if exp := toInt(decodeJWTPayload(strings.TrimSpace(accessToken))["exp"]); exp > 0 {
		return sql.NullTime{Time: time.Unix(int64(exp), 0), Valid: true}
	}
	return stored
}

func sunnyPreferredAccessToken(values ...string) string {
	best := ""
	bestExpiry := 0
	for _, value := range values {
		token := strings.TrimSpace(value)
		if token == "" {
			continue
		}
		expiry := toInt(decodeJWTPayload(token)["exp"])
		if best == "" || expiry > bestExpiry {
			best = token
			bestExpiry = expiry
		}
	}
	return best
}

type sunnySessionListRow struct {
	ID                   uint         `gorm:"column:id"`
	AccountID            uint         `gorm:"column:account_id"`
	Email                string       `gorm:"column:email"`
	AccessToken          string       `gorm:"column:access_token"`
	SessionJSON          string       `gorm:"column:session_json"`
	AccessTokenStatus    string       `gorm:"column:access_token_status"`
	AccessTokenError     string       `gorm:"column:access_token_error"`
	AccessTokenCheckedAt *time.Time   `gorm:"column:access_token_checked_at"`
	HealthCheckStatus    string       `gorm:"column:health_check_status"`
	HealthCheckError     string       `gorm:"column:health_check_error"`
	ExpiresAt            sql.NullTime `gorm:"column:expires_at"`
	HasAccessToken       int          `gorm:"column:has_access_token"`
	HasRefreshToken      int          `gorm:"column:has_refresh_token"`
	HasSecretKey         int          `gorm:"column:has_secret_key"`
	UpdatedAt            time.Time    `gorm:"column:updated_at"`
}

type sunnySessionAccountSummary struct {
	ID                      uint       `gorm:"column:id"`
	MailboxID               uint       `gorm:"column:mailbox_id"`
	Email                   string     `gorm:"column:email"`
	Status                  string     `gorm:"column:status"`
	AccountType             string     `gorm:"column:account_type"`
	TrialEligibility        string     `gorm:"column:trial_eligibility"`
	TrialCountryResultsJSON string     `gorm:"column:trial_country_results_json"`
	TrialCheckedAt          *time.Time `gorm:"column:trial_checked_at"`
	CheckoutKind            string     `gorm:"column:checkout_kind"`
	CheckoutResultJSON      string     `gorm:"column:checkout_result_json"`
	PaymentMethodsJSON      string     `gorm:"column:payment_methods_json"`
	PaymentProbeMethodsJSON string     `gorm:"column:payment_probe_methods_json"`
	PaymentProbeResultsJSON string     `gorm:"column:payment_probe_results_json"`
	PaymentProbeError       string     `gorm:"column:payment_probe_error"`
	PaymentProbedAt         *time.Time `gorm:"column:payment_probed_at"`
	CommerceCheckError      string     `gorm:"column:commerce_check_error"`
	CommerceCheckedAt       *time.Time `gorm:"column:commerce_checked_at"`
	AccessToken             string     `gorm:"column:access_token"`
	PhoneNumber             string     `gorm:"column:phone_number"`
	HasAccessToken          int        `gorm:"column:has_access_token"`
	HasRefreshToken         int        `gorm:"column:has_refresh_token"`
	LastHealthCheckedAt     *time.Time `gorm:"column:last_health_checked_at"`
	RebindEmail             string     `gorm:"column:rebind_email"`
}

type sunnySessionMailboxSummary struct {
	ID                      uint       `gorm:"column:id"`
	Email                   string     `gorm:"column:email"`
	RebindEmail             string     `gorm:"column:rebind_email"`
	Status                  string     `gorm:"column:status"`
	AccountType             string     `gorm:"column:account_type"`
	TrialEligibility        string     `gorm:"column:trial_eligibility"`
	TrialCountryResultsJSON string     `gorm:"column:trial_country_results_json"`
	TrialCheckedAt          *time.Time `gorm:"column:trial_checked_at"`
	HasSecretKey            int        `gorm:"column:has_secret_key"`
	HasChatGPTPassword      int        `gorm:"column:has_chatgpt_password"`
	HasTOTPSecret           int        `gorm:"column:has_totp_secret"`
	ChatGPTPassword         string     `gorm:"column:chat_gpt_password"`
	TOTPSecret              string     `gorm:"column:totp_secret"`
	Raw                     string     `gorm:"column:raw"`
	GroupID                 uint       `gorm:"column:group_id"`
	GroupName               string     `gorm:"column:group_name"`
	LastHealthCheckedAt     *time.Time `gorm:"column:last_health_checked_at"`
}

const sunnySessionListColumns = `id, account_id, email, access_token, access_token_status, access_token_error, access_token_checked_at, health_check_status, health_check_error, expires_at, updated_at,
	session_json,
	CASE WHEN access_token IS NOT NULL AND access_token <> '' THEN 1 ELSE 0 END AS has_access_token,
	CASE WHEN refresh_token IS NOT NULL AND refresh_token <> '' THEN 1 ELSE 0 END AS has_refresh_token,
	CASE WHEN raw_mailbox_line IS NOT NULL AND raw_mailbox_line <> '' THEN 1 ELSE 0 END AS has_secret_key`

func serializeSunnySessionList(row sunnySessionListRow, accounts map[string]sunnySessionAccountSummary, mailboxes map[string]sunnySessionMailboxSummary) map[string]any {
	key := sunnyEmailKey(row.Email)
	account := accounts[key]
	mailbox := mailboxes[key]
	displayEmail := strings.TrimSpace(row.Email)
	if mailboxEmail := strings.TrimSpace(mailbox.Email); mailboxEmail != "" {
		displayEmail = mailboxEmail
	}
	statusSource := account.Status
	if mailbox.ID != 0 {
		statusSource = mailbox.Status
		if strings.TrimSpace(statusSource) == "" {
			statusSource = "unused"
		}
	}
	status := normalizeSunnyDisplayStatus(statusSource)
	plan := normalizeSunnyPlanType(account.AccountType)
	if mailboxPlan := normalizeSunnyPlanType(mailbox.AccountType); mailboxPlan != "" && mailboxPlan != "free" {
		plan = mailboxPlan
	} else if plan == "" {
		plan = mailboxPlan
	}
	if plan == "" && row.ID != 0 {
		plan = "free"
	}
	lastHealthCheckedAt := account.LastHealthCheckedAt
	if mailbox.LastHealthCheckedAt != nil {
		lastHealthCheckedAt = mailbox.LastHealthCheckedAt
	}
	lastHealthText := ""
	if lastHealthCheckedAt != nil {
		lastHealthText = formatTime(*lastHealthCheckedAt)
	}
	expiresAt := sunnyAccessTokenExpiry(sunnyPreferredAccessToken(row.AccessToken, sunnyAccessTokenFromSessionJSON(row.SessionJSON), account.AccessToken), row.ExpiresAt)
	trialEligibility := sunnyTrialEligibilityFor(account.TrialEligibility, mailbox.TrialEligibility)
	checkoutKind := normalizeSunnyCheckoutKind(account.CheckoutKind)
	paymentMethods := []string{}
	paymentMethodsJSON := account.PaymentMethodsJSON
	if account.PaymentProbedAt != nil {
		paymentMethodsJSON = account.PaymentProbeMethodsJSON
	}
	if err := json.Unmarshal([]byte(fallback(paymentMethodsJSON, "[]")), &paymentMethods); err != nil {
		paymentMethods = []string{}
	}
	paymentProbeResults := map[string]any{}
	if err := json.Unmarshal([]byte(fallback(account.PaymentProbeResultsJSON, "{}")), &paymentProbeResults); err != nil {
		paymentProbeResults = map[string]any{}
	}
	trialCheckedAt := account.TrialCheckedAt
	if trialCheckedAt == nil {
		trialCheckedAt = mailbox.TrialCheckedAt
	}
	accountID := row.AccountID
	if accountID == 0 {
		accountID = account.ID
	}
	hasSecretKey := row.HasSecretKey != 0
	if mailbox.ID != 0 {
		hasSecretKey = mailbox.HasSecretKey != 0
	}
	return map[string]any{
		"id": row.ID, "account_id": accountID, "mailbox_id": mailbox.ID, "email": displayEmail,
		"status": status, "plan_type": plan, "trial_eligibility": trialEligibility, "group_id": mailbox.GroupID, "group_name": mailbox.GroupName,
		"trial_country_results": sunnyTrialCountryResults(account.TrialCountryResultsJSON, mailbox.TrialCountryResultsJSON),
		"checkout_kind":         checkoutKind, "checkout_result": sunnyCheckoutResultJSON(account.CheckoutResultJSON), "payment_methods": paymentMethods, "payment_probe_results": paymentProbeResults,
		"payment_probe_error": account.PaymentProbeError, "payment_probed_at": nullableTime(account.PaymentProbedAt != nil, pointerTime(account.PaymentProbedAt)), "commerce_check_error": account.CommerceCheckError,
		"phone_bound":          sunnyPhoneBindingCompleted(account.PhoneNumber, account.Status, mailbox.Status),
		"rebind_email":         fallback(strings.TrimSpace(mailbox.RebindEmail), strings.TrimSpace(account.RebindEmail)),
		"has_access_token":     row.HasAccessToken != 0 || account.HasAccessToken != 0,
		"has_refresh_token":    row.HasRefreshToken != 0 || account.HasRefreshToken != 0,
		"has_secret_key":       hasSecretKey,
		"has_chatgpt_password": mailbox.HasChatGPTPassword != 0,
		"has_totp_secret":      mailbox.HasTOTPSecret != 0,
		"has_login_secret":     mailbox.HasChatGPTPassword != 0 && mailbox.HasTOTPSecret != 0,
		"updated_at":           formatTime(row.UpdatedAt), "access_token_expires_at": nullableTime(expiresAt.Valid, expiresAt.Time), "last_health_checked_at": lastHealthText,
		"access_token_status": fallback(row.AccessTokenStatus, "unknown"), "access_token_error": row.AccessTokenError,
		"access_token_checked_at": nullableTime(row.AccessTokenCheckedAt != nil, sunnyTimePointerValue(row.AccessTokenCheckedAt)),
		"health_check_status":     fallback(row.HealthCheckStatus, "unknown"), "health_check_error": row.HealthCheckError,
		"trial_checked_at":    nullableTime(trialCheckedAt != nil, pointerTime(trialCheckedAt)),
		"commerce_checked_at": nullableTime(account.CommerceCheckedAt != nil, pointerTime(account.CommerceCheckedAt)),
	}
}

func sunnyTimePointerValue(value *time.Time) time.Time {
	if value == nil {
		return time.Time{}
	}
	return *value
}

func sunnyTrialCountryResults(accountJSON, mailboxJSON string) map[string]string {
	result := map[string]string{}
	for _, raw := range []string{accountJSON, mailboxJSON} {
		var values map[string]string
		if json.Unmarshal([]byte(raw), &values) != nil {
			continue
		}
		for country, eligibility := range values {
			country = strings.ToUpper(strings.TrimSpace(country))
			eligibility = normalizeSunnyTrialEligibility(eligibility)
			if country != "" && eligibility != sunnyTrialUnknown {
				result[country] = eligibility
			}
		}
	}
	return result
}

func normalizeSunnyTrialCountryFilter(value string) []string {
	countries := make([]string, 0)
	seen := map[string]bool{}
	for _, part := range strings.FieldsFunc(value, func(char rune) bool {
		return char == ',' || char == ';' || char == '|'
	}) {
		country, err := normalizeSunnyProxyCountry(part)
		if err == nil && !seen[country] {
			seen[country] = true
			countries = append(countries, country)
		}
	}
	sort.Strings(countries)
	return countries
}

func normalizeSunnyRebindEmailFilter(value string) string {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "present", "has", "1", "true", "已换绑":
		return "present"
	case "missing", "none", "0", "false", "未换绑":
		return "missing"
	default:
		return ""
	}
}

func sunnyHasAllEligibleTrialCountries(value any, required []string) bool {
	if len(required) == 0 {
		return true
	}
	results, ok := value.(map[string]string)
	if !ok {
		return false
	}
	for _, country := range required {
		if normalizeSunnyTrialEligibility(results[country]) != sunnyTrialEligible {
			return false
		}
	}
	return true
}

func (s *Server) sunnyTrialCountryOptions() []string {
	type row struct {
		TrialCountryResultsJSON string `gorm:"column:trial_country_results_json"`
	}
	rows := make([]row, 0)
	var accountRows []row
	if s.db.Model(&SunnyAccount{}).Select("trial_country_results_json").Find(&accountRows).Error == nil {
		rows = append(rows, accountRows...)
	}
	var mailboxRows []row
	if s.db.Model(&SunnyMailbox{}).Select("trial_country_results_json").Find(&mailboxRows).Error == nil {
		rows = append(rows, mailboxRows...)
	}
	seen := map[string]bool{}
	for _, item := range rows {
		for country := range sunnyTrialCountryResults(item.TrialCountryResultsJSON, "{}") {
			if normalized, err := normalizeSunnyProxyCountry(country); err == nil {
				seen[normalized] = true
			}
		}
	}
	options := make([]string, 0, len(seen))
	for country := range seen {
		options = append(options, country)
	}
	sort.Strings(options)
	return options
}

// sunnyPaymentMethodOptions aggregates every normalized method already saved
// by payment/checkout probes. It intentionally does not use a fixed allowlist:
// new country-specific methods should become filterable as soon as they are
// returned by the upstream checkout API.
func (s *Server) sunnyPaymentMethodOptions() []string {
	var rows []struct {
		PaymentMethodsJSON      string `gorm:"column:payment_methods_json"`
		PaymentProbeMethodsJSON string `gorm:"column:payment_probe_methods_json"`
	}
	if err := s.db.Model(&SunnyAccount{}).Select("payment_methods_json, payment_probe_methods_json").Find(&rows).Error; err != nil {
		return nil
	}
	methods := make([]string, 0)
	for _, row := range rows {
		for _, raw := range []string{row.PaymentMethodsJSON, row.PaymentProbeMethodsJSON} {
			var values []string
			if json.Unmarshal([]byte(raw), &values) == nil {
				methods = append(methods, values...)
			}
		}
	}
	return normalizeSunnyPaymentMethods(methods)
}

func (s *Server) sunnySessionSidecars(rows []SunnySession) (map[string]SunnyAccount, map[string]SunnyMailbox) {
	emails := []string{}
	accountIDs := []uint{}
	for _, row := range rows {
		emails = append(emails, row.Email)
		if row.AccountID != 0 {
			accountIDs = append(accountIDs, row.AccountID)
		}
	}
	accounts := map[string]SunnyAccount{}
	mailboxes := map[string]SunnyMailbox{}
	if len(emails) == 0 {
		return accounts, mailboxes
	}
	var accRows []SunnyAccount
	query := s.db.Where("email IN ?", emails)
	if len(accountIDs) > 0 {
		query = query.Or("id IN ?", accountIDs)
	}
	query.Find(&accRows)
	accountByID := map[uint]SunnyAccount{}
	for _, a := range accRows {
		accounts[sunnyEmailKey(a.Email)] = a
		accountByID[a.ID] = a
	}
	mailboxIDs := []uint{}
	for _, account := range accRows {
		if account.MailboxID != 0 {
			mailboxIDs = append(mailboxIDs, account.MailboxID)
		}
	}
	var mbRows []SunnyMailbox
	mailboxQuery := s.db.Where("email IN ? OR rebind_email IN ?", emails, emails)
	if len(mailboxIDs) > 0 {
		mailboxQuery = mailboxQuery.Or("id IN ?", mailboxIDs)
	}
	mailboxQuery.Find(&mbRows)
	mailboxByID := map[uint]SunnyMailbox{}
	for _, m := range mbRows {
		mailboxes[sunnyEmailKey(m.Email)] = m
		if m.RebindEmail != "" {
			mailboxes[sunnyEmailKey(m.RebindEmail)] = m
		}
		mailboxByID[m.ID] = m
	}
	for _, row := range rows {
		key := sunnyEmailKey(row.Email)
		account := accountByID[row.AccountID]
		if account.ID != 0 {
			accounts[key] = account
			if mailbox := mailboxByID[account.MailboxID]; mailbox.ID != 0 {
				mailboxes[key] = mailbox
			}
		}
	}
	return accounts, mailboxes
}

func (s *Server) sunnySessionListSidecars(rows []sunnySessionListRow) (map[string]sunnySessionAccountSummary, map[string]sunnySessionMailboxSummary) {
	emails := make([]string, 0, len(rows))
	for _, row := range rows {
		emails = append(emails, row.Email)
	}
	accounts := map[string]sunnySessionAccountSummary{}
	mailboxes := map[string]sunnySessionMailboxSummary{}
	if len(emails) == 0 {
		return accounts, mailboxes
	}
	var accRows []sunnySessionAccountSummary
	accountIDs := make([]uint, 0, len(rows))
	for _, row := range rows {
		if row.AccountID != 0 {
			accountIDs = append(accountIDs, row.AccountID)
		}
	}
	accountQuery := s.db.Model(&SunnyAccount{}).Select(`id, mailbox_id, email, status, account_type, trial_eligibility, trial_country_results_json, trial_checked_at, checkout_kind, checkout_result_json, payment_methods_json, payment_probe_methods_json, payment_probe_results_json, payment_probe_error, payment_probed_at, commerce_check_error, commerce_checked_at, access_token, phone_number, last_health_checked_at, rebind_email,
		CASE WHEN access_token IS NOT NULL AND access_token <> '' THEN 1 ELSE 0 END AS has_access_token,
		CASE WHEN openai_rt IS NOT NULL AND openai_rt <> '' THEN 1 ELSE 0 END AS has_refresh_token`).Where("email IN ?", emails)
	if len(accountIDs) > 0 {
		accountQuery = accountQuery.Or("id IN ?", accountIDs)
	}
	accountQuery.Find(&accRows)
	accountByID := map[uint]sunnySessionAccountSummary{}
	for _, account := range accRows {
		accounts[sunnyEmailKey(account.Email)] = account
		accountByID[account.ID] = account
	}
	mailboxIDs := []uint{}
	for _, account := range accRows {
		if account.MailboxID != 0 {
			mailboxIDs = append(mailboxIDs, account.MailboxID)
		}
	}
	var mailboxRows []sunnySessionMailboxSummary
	mailboxQuery := s.db.Model(&SunnyMailbox{}).Select(`sunny_mailboxes.id, sunny_mailboxes.email, sunny_mailboxes.rebind_email, sunny_mailboxes.status, sunny_mailboxes.account_type, sunny_mailboxes.trial_eligibility, sunny_mailboxes.trial_country_results_json, sunny_mailboxes.trial_checked_at,
		sunny_mailboxes.group_id, sunny_mailboxes.last_health_checked_at, sunny_mailbox_groups.name AS group_name, sunny_mailboxes.chat_gpt_password, sunny_mailboxes.totp_secret, sunny_mailboxes.raw,
		CASE
			WHEN COALESCE(sunny_mailboxes.rebind_email, '') <> '' AND COALESCE(sunny_mailboxes.rebind_mailbox_api, '') <> '' THEN 1
			WHEN COALESCE(sunny_mailboxes.raw, '') <> '' THEN 1
			WHEN LOWER(sunny_mailboxes.mailbox_type) IN ('apple', 'icloud') AND LOWER(sunny_mailboxes.mailbox_channel) IN ('url_api', 'url-api') AND sunny_mailboxes.email <> '' THEN 1
			WHEN LOWER(sunny_mailboxes.mailbox_type) IN ('apple', 'icloud') AND sunny_mailboxes.email <> '' AND sunny_mailboxes.access_key <> '' THEN 1
			WHEN sunny_mailboxes.email <> '' AND sunny_mailboxes.password <> '' AND sunny_mailboxes.client_id <> '' AND sunny_mailboxes.refresh_token <> '' THEN 1
			ELSE 0
		END AS has_secret_key,
		CASE WHEN coalesce(sunny_mailboxes.chat_gpt_password,'') <> '' THEN 1 ELSE 0 END AS has_chatgpt_password,
		CASE WHEN coalesce(sunny_mailboxes.totp_secret,'') <> '' THEN 1 ELSE 0 END AS has_totp_secret`).
		Joins("LEFT JOIN sunny_mailbox_groups ON sunny_mailbox_groups.id = sunny_mailboxes.group_id").
		Where("sunny_mailboxes.email IN ? OR sunny_mailboxes.rebind_email IN ?", emails, emails)
	if len(mailboxIDs) > 0 {
		mailboxQuery = mailboxQuery.Or("sunny_mailboxes.id IN ?", mailboxIDs)
	}
	mailboxQuery.Find(&mailboxRows)
	mailboxByID := map[uint]sunnySessionMailboxSummary{}
	for _, mailbox := range mailboxRows {
		mailboxes[sunnyEmailKey(mailbox.Email)] = mailbox
		if mailbox.RebindEmail != "" {
			mailboxes[sunnyEmailKey(mailbox.RebindEmail)] = mailbox
		}
		mailboxByID[mailbox.ID] = mailbox
	}
	for _, row := range rows {
		key := sunnyEmailKey(row.Email)
		account := accountByID[row.AccountID]
		if account.ID != 0 {
			accounts[key] = account
			if mailbox := mailboxByID[account.MailboxID]; mailbox.ID != 0 {
				mailboxes[key] = mailbox
			}
		}
	}
	return accounts, mailboxes
}

func (s *Server) sunnySessionFieldValue(id uint, field string) (string, error) {
	var sess SunnySession
	query := s.db.Model(&SunnySession{}).Where("id = ?", id)
	switch field {
	case "access_token":
		if err := query.Select("id", "account_id", "email", "access_token", "session_json").First(&sess).Error; err != nil {
			return "", fmt.Errorf("session not found")
		}
		if mailbox, err := s.sunnyMailboxForSession(sess); err == nil {
			return s.sunnyMailboxAccessToken(mailbox), nil
		}
		value := sunnyPreferredAccessToken(sess.AccessToken, sunnyAccessTokenFromSessionJSON(sess.SessionJSON))
		return value, nil
	case "refresh_token":
		if err := query.Select("id", "email", "refresh_token").First(&sess).Error; err != nil {
			return "", fmt.Errorf("session not found")
		}
		value := sess.RefreshToken
		if value == "" {
			var account SunnyAccount
			s.db.Select("openai_rt").Where("email = ?", sess.Email).First(&account)
			value = account.OpenAIRT
		}
		return value, nil
	case "secret_key":
		if err := query.Select("id", "account_id", "email", "raw_mailbox_line").First(&sess).Error; err != nil {
			return "", fmt.Errorf("session not found")
		}
		if mailbox, err := s.sunnyMailboxForSession(sess); err == nil {
			if value := sunnyMailboxCredentialLine(mailbox); value != "" {
				return value, nil
			}
		}
		return sess.RawMailboxLine, nil
	case "chatgpt_password", "totp_secret", "login_secret":
		if err := query.Select("id", "account_id", "email").First(&sess).Error; err != nil {
			return "", fmt.Errorf("session not found")
		}
		mailbox, err := s.sunnyMailboxForSession(sess)
		if err != nil {
			return "", fmt.Errorf("mailbox not found")
		}
		switch field {
		case "chatgpt_password":
			return mailbox.ChatGPTPassword, nil
		case "totp_secret":
			return mailbox.TOTPSecret, nil
		default:
			return sunnyLoginSecretLine(mailbox), nil
		}
	default:
		return "", fmt.Errorf("unsupported session field")
	}
}

func (s *Server) sunnyMailboxForSession(sess SunnySession) (SunnyMailbox, error) {
	var account SunnyAccount
	if sess.AccountID != 0 {
		s.db.Select("id", "mailbox_id").First(&account, sess.AccountID)
	}
	if account.ID == 0 {
		s.db.Select("id", "mailbox_id").Where("LOWER(email) = ?", sunnyEmailKey(sess.Email)).First(&account)
	}
	var mailbox SunnyMailbox
	if account.MailboxID != 0 && s.db.First(&mailbox, account.MailboxID).Error == nil {
		return mailbox, nil
	}
	err := s.db.Where("LOWER(email) = ? OR LOWER(rebind_email) = ?", sunnyEmailKey(sess.Email), sunnyEmailKey(sess.Email)).First(&mailbox).Error
	return mailbox, err
}

func (s *Server) sunnySessions(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) == 0 && r.Method == http.MethodGet {
		q := r.URL.Query()
		paymentMethodOptions := s.sunnyPaymentMethodOptions()
		trialCountryOptions := s.sunnyTrialCountryOptions()
		page := intValue(q.Get("page"), 1)
		if page < 1 {
			page = 1
		}
		pageSize := intValue(q.Get("page_size"), 10)
		if pageSize < 1 {
			pageSize = 10
		}
		if pageSize > 100 {
			pageSize = 100
		}
		kw := strings.ToLower(strings.TrimSpace(q.Get("q")))
		statusFilter := strings.TrimSpace(q.Get("status"))
		planFilter := strings.ToLower(strings.TrimSpace(q.Get("plan_type")))
		trialFilter := normalizeSunnyTrialFilter(q.Get("trial_eligibility"))
		checkoutFilter := normalizeSunnyCheckoutFilter(q.Get("checkout_kind"))
		paymentMethodFilter := normalizeSunnyPaymentMethodFilter(q.Get("payment_methods"))
		paymentProbeFilter := normalizeSunnyPaymentProbeFilter(q.Get("payment_probe_status"))
		loginSecretFilter := normalizeSunnyLoginSecretFilter(q.Get("login_secret"))
		rebindEmailFilter := normalizeSunnyRebindEmailFilter(q.Get("rebind_email"))
		trialCountryFilter := normalizeSunnyTrialCountryFilter(q.Get("trial_countries"))
		groupFilter := uint(intValue(q.Get("group_id"), 0))
		sortBy := strings.ToLower(strings.TrimSpace(q.Get("sort_by")))
		if statusFilter == "" && planFilter == "" && trialFilter == "" && checkoutFilter == "" && len(paymentMethodFilter) == 0 && paymentProbeFilter == "" && loginSecretFilter == "" && rebindEmailFilter == "" && len(trialCountryFilter) == 0 && sortBy != "rebind_email" {
			query := s.db.Model(&SunnySession{})
			query = sunnyUniqueSessionIdentityScope(query)
			if kw != "" {
				pattern := "%" + kw + "%"
				rebindEmails := s.db.Model(&SunnyAccount{}).Select("email").Where("LOWER(rebind_email) LIKE ?", pattern)
				query = query.Where("LOWER(sunny_sessions.email) LIKE ? OR sunny_sessions.email IN (?)", pattern, rebindEmails)
			}
			if groupFilter != 0 {
				mailboxEmails := s.db.Model(&SunnyMailbox{}).Select("email").Where("group_id = ?", groupFilter)
				query = query.Where("email IN (?)", mailboxEmails)
			}
			if strings.EqualFold(strings.TrimSpace(q.Get("selection")), "all") {
				var ids []uint
				query.Order("id desc").Pluck("id", &ids)
				writeJSON(w, 200, map[string]any{"ids": ids, "total": len(ids), "payment_method_options": paymentMethodOptions, "trial_country_options": trialCountryOptions})
				return
			}
			var total int64
			query.Count(&total)
			orderClause := sunnySortClause(q.Get("sort_by"), q.Get("sort_order"), map[string]string{"updated_at": "updated_at", "created_at": "created_at", "last_refresh_at": "last_refresh_at", "access_token_expires_at": "expires_at"}, "updated_at desc")
			if sortBy == "last_health_checked_at" {
				order := "DESC"
				if strings.EqualFold(q.Get("sort_order"), "asc") {
					order = "ASC"
				}
				orderClause = `COALESCE(
					(SELECT last_health_checked_at FROM sunny_mailboxes WHERE sunny_mailboxes.email = sunny_sessions.email),
					(SELECT last_health_checked_at FROM sunny_accounts WHERE sunny_accounts.email = sunny_sessions.email)
				) ` + order + ", sunny_sessions.id DESC"
			}
			var rows []sunnySessionListRow
			query.Select(sunnySessionListColumns).Order(orderClause).Offset((page - 1) * pageSize).Limit(pageSize).Scan(&rows)
			accounts, mailboxes := s.sunnySessionListSidecars(rows)
			items := make([]map[string]any, 0, len(rows))
			for _, row := range rows {
				items = append(items, serializeSunnySessionList(row, accounts, mailboxes))
			}
			writeJSON(w, 200, map[string]any{"items": items, "total": total, "page": page, "page_size": pageSize, "payment_method_options": paymentMethodOptions, "trial_country_options": trialCountryOptions})
			return
		}
		var rows []sunnySessionListRow
		listQuery := sunnyUniqueSessionIdentityScope(s.db.Model(&SunnySession{}))
		listQuery.Select(sunnySessionListColumns).Scan(&rows)
		accounts, mailboxes := s.sunnySessionListSidecars(rows)
		itemsAll := []map[string]any{}
		for _, row := range rows {
			item := serializeSunnySessionList(row, accounts, mailboxes)
			if kw != "" && !strings.Contains(strings.ToLower(text(item["email"])), kw) && !strings.Contains(strings.ToLower(text(item["rebind_email"])), kw) {
				continue
			}
			if statusFilter != "" && text(item["status"]) != statusFilter {
				continue
			}
			if planFilter != "" && strings.ToLower(text(item["plan_type"])) != planFilter {
				continue
			}
			if trialFilter != "" {
				if !sunnyTrialApplies(text(item["status"]), text(item["plan_type"])) || normalizeSunnyTrialEligibility(text(item["trial_eligibility"])) != trialFilter {
					continue
				}
			}
			if checkoutFilter != "" && normalizeSunnyCheckoutKind(text(item["checkout_kind"])) != checkoutFilter {
				continue
			}
			if loginSecretFilter != "" && (loginSecretFilter == "present") != boolValue(item["has_login_secret"], false) {
				continue
			}
			if rebindEmailFilter != "" && (rebindEmailFilter == "present") != (strings.TrimSpace(text(item["rebind_email"])) != "") {
				continue
			}
			if !sunnyHasAllEligibleTrialCountries(item["trial_country_results"], trialCountryFilter) {
				continue
			}
			if !sunnyHasAllPaymentMethods(item["payment_methods"], paymentMethodFilter) {
				continue
			}
			if paymentProbeFilter == "unknown" && strings.TrimSpace(text(item["payment_probed_at"])) != "" {
				continue
			}
			if groupFilter != 0 && uint(intValue(item["group_id"], 0)) != groupFilter {
				continue
			}
			itemsAll = append(itemsAll, item)
		}
		if strings.EqualFold(strings.TrimSpace(q.Get("selection")), "all") {
			ids := make([]uint, 0, len(itemsAll))
			for _, item := range itemsAll {
				if id := uint(intValue(item["id"], 0)); id != 0 {
					ids = append(ids, id)
				}
			}
			writeJSON(w, 200, map[string]any{"ids": ids, "total": len(ids), "payment_method_options": paymentMethodOptions, "trial_country_options": trialCountryOptions})
			return
		}
		if sortBy == "" {
			sortBy = "last_health_checked_at"
		}
		desc := strings.ToLower(q.Get("sort_order")) != "asc"
		sort.SliceStable(itemsAll, func(i, j int) bool {
			a, b := text(itemsAll[i][sortBy]), text(itemsAll[j][sortBy])
			if sortBy == "rebind_email" {
				a = strings.ToLower(strings.TrimSpace(a))
				b = strings.ToLower(strings.TrimSpace(b))
				aRebound, bRebound := a != "", b != ""
				if aRebound != bRebound {
					if desc {
						return aRebound
					}
					return !aRebound
				}
				if a != b {
					if desc {
						return a > b
					}
					return a < b
				}
				return intValue(itemsAll[i]["id"], 0) > intValue(itemsAll[j]["id"], 0)
			}
			if desc {
				return a > b
			}
			return a < b
		})
		total := len(itemsAll)
		start := (page - 1) * pageSize
		if start > total {
			start = total
		}
		end := start + pageSize
		if end > total {
			end = total
		}
		writeJSON(w, 200, map[string]any{"items": itemsAll[start:end], "total": total, "page": page, "page_size": pageSize, "payment_method_options": paymentMethodOptions, "trial_country_options": trialCountryOptions})
		return
	}
	if len(parts) == 1 && parts[0] == "health-check" && r.Method == http.MethodPost {
		body, err := parseBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		task, err := s.createSunnyHealthTask(body)
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, serializeTask(task))
		return
	}
	if len(parts) == 1 && parts[0] == "subscription-check" && r.Method == http.MethodPost {
		body, err := parseBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		task, err := s.createSunnySubscriptionTask(body)
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, serializeTask(task))
		return
	}
	if len(parts) == 1 && parts[0] == "trial-check" && r.Method == http.MethodPost {
		body, err := parseBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		task, err := s.createSunnyTrialTask(body)
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, serializeTask(task))
		return
	}
	if len(parts) == 2 && parts[0] == "trial-check" && parts[1] == "countries" && r.Method == http.MethodGet {
		groups, err := s.sunnyCommerceProxyGroups()
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"countries": sunnyCommerceProxyCountryList(groups)})
		return
	}
	if len(parts) == 1 && parts[0] == "checkout-probe" && r.Method == http.MethodPost {
		body, err := parseBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		task, err := s.createSunnyCheckoutProbeTask(body)
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, serializeTask(task))
		return
	}
	if len(parts) == 2 && parts[0] == "payment-probe" && parts[1] == "countries" && r.Method == http.MethodGet {
		groups, err := s.sunnyPaymentProxyGroups()
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"countries": sunnyPaymentProbeCountryList(groups)})
		return
	}
	if len(parts) == 1 && parts[0] == "payment-probe" && r.Method == http.MethodPost {
		body, err := parseBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		task, err := s.createSunnyPaymentProbeTask(body)
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, serializeTask(task))
		return
	}
	if len(parts) == 1 && parts[0] == "access-token-check" && r.Method == http.MethodPost {
		body, err := parseBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		task, err := s.createSunnyAccessTokenCheckTask(body)
		if err != nil {
			writeError(w, http.StatusConflict, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, serializeTask(task))
		return
	}
	if len(parts) == 1 && parts[0] == "rebind" && r.Method == http.MethodPost {
		body, err := parseBody(r)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		task, err := s.createSunnyRebindTask(body)
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		writeJSON(w, http.StatusAccepted, serializeTask(task))
		return
	}
	if len(parts) == 1 && parts[0] == "export" && r.Method == http.MethodPost {
		body, _ := parseBody(r)
		format := fallback(text(body["format"]), "json")
		var rows []SunnySession
		sessionIDs := uintSlice(body["session_ids"])
		accountIDs := uintSlice(body["account_ids"])
		emails := stringSlice(body["emails"])
		q := s.db.Model(&SunnySession{})
		if len(sessionIDs) > 0 {
			q = q.Where("id IN ?", sessionIDs)
		} else if len(accountIDs) > 0 {
			q = q.Where("account_id IN ?", accountIDs)
		} else if len(emails) > 0 {
			q = q.Where("email IN ?", emails)
		}
		q.Order("id asc").Find(&rows)
		s.sunnyExportSessions(w, rows, format)
		return
	}
	if len(parts) == 2 && parts[1] == "field" && r.Method == http.MethodGet {
		id := uint(intValue(parts[0], 0))
		field := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("name")))
		if id == 0 {
			writeError(w, 400, "invalid session id")
			return
		}
		value, err := s.sunnySessionFieldValue(id, field)
		if err != nil {
			status := http.StatusBadRequest
			if err.Error() == "session not found" {
				status = http.StatusNotFound
			}
			writeError(w, status, err.Error())
			return
		}
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("Pragma", "no-cache")
		writeJSON(w, 200, map[string]any{"field": field, "value": value})
		return
	}
	if len(parts) == 1 {
		id := uint(intValue(parts[0], 0))
		var sess SunnySession
		if id == 0 || s.db.First(&sess, id).Error != nil {
			writeError(w, 404, "session not found")
			return
		}
		if r.Method == http.MethodGet {
			w.Header().Set("Cache-Control", "no-store")
			w.Header().Set("Pragma", "no-cache")
			accounts, mailboxes := s.sunnySessionSidecars([]SunnySession{sess})
			writeJSON(w, 200, s.serializeSunnySession(sess, accounts, mailboxes))
			return
		}
		if r.Method == http.MethodPut {
			body, _ := parseBody(r)
			originalEmail := sess.Email
			targetEmail := originalEmail
			if _, ok := body["email"]; ok {
				var err error
				targetEmail, err = normalizeSunnyEditableEmail(text(body["email"]))
				if err != nil {
					writeError(w, http.StatusUnprocessableEntity, err.Error())
					return
				}
			}
			rebindEmail, rebindEmailProvided := "", false
			rebindAPI, rebindAPIProvided := "", false
			if _, ok := body["rebind_email"]; ok {
				rebindEmailProvided = true
				rebindEmail = strings.TrimSpace(text(body["rebind_email"]))
				if rebindEmail != "" {
					var err error
					rebindEmail, err = normalizeSunnyEditableEmail(rebindEmail)
					if err != nil {
						writeError(w, http.StatusUnprocessableEntity, err.Error())
						return
					}
				}
			}
			if _, ok := body["rebind_mailbox_api"]; ok {
				rebindAPIProvided = true
				rebindAPI = strings.TrimSpace(text(body["rebind_mailbox_api"]))
			}
			if (rebindEmailProvided || rebindAPIProvided) && ((rebindEmail == "") != (rebindAPI == "")) {
				writeError(w, http.StatusUnprocessableEntity, "换绑邮箱名和换绑邮箱 API 必须同时填写或同时清空")
				return
			}
			if rebindEmail != "" {
				if err := validateDomainMailboxAccessKey(rebindAPI, rebindEmail); err != nil {
					writeError(w, http.StatusUnprocessableEntity, err.Error())
					return
				}
			}
			if sunnyEmailKey(originalEmail) != sunnyEmailKey(targetEmail) {
				if err := sunnyEmailRenameConflict(s.db, targetEmail); err != nil {
					if strings.Contains(err.Error(), "已被其他") {
						writeError(w, http.StatusConflict, err.Error())
					} else {
						writeError(w, http.StatusInternalServerError, err.Error())
					}
					return
				}
			}
			emailChanged := originalEmail != targetEmail
			sess.Email = targetEmail
			status := strings.TrimSpace(text(body["status"]))
			planType := ""
			if _, ok := body["plan_type"]; ok {
				planType = normalizeSunnyPlanType(text(body["plan_type"]))
				allowedPlans := map[string]bool{"free": true, "plus": true, "k12": true, "team": true, "pro": true}
				if !allowedPlans[planType] {
					writeError(w, 400, "invalid plan type")
					return
				}
			}
			var targetGroup *SunnyMailboxGroup
			if _, ok := body["group_id"]; ok {
				groupID := uint(intValue(body["group_id"], 0))
				var group SunnyMailboxGroup
				if groupID == 0 || s.db.First(&group, groupID).Error != nil {
					writeError(w, 400, "mailbox group not found")
					return
				}
				targetGroup = &group
			}
			now := time.Now()
			if err := s.db.Transaction(func(tx *gorm.DB) error {
				var linkedAccount SunnyAccount
				if sess.AccountID != 0 {
					tx.First(&linkedAccount, sess.AccountID)
				}
				if linkedAccount.ID == 0 {
					tx.Where("LOWER(email) = ?", sunnyEmailKey(originalEmail)).First(&linkedAccount)
				}
				var linkedMailbox SunnyMailbox
				if linkedAccount.MailboxID != 0 {
					tx.First(&linkedMailbox, linkedAccount.MailboxID)
				}
				if linkedMailbox.ID == 0 {
					tx.Where("LOWER(email) = ? OR LOWER(rebind_email) = ?", sunnyEmailKey(originalEmail), sunnyEmailKey(originalEmail)).First(&linkedMailbox)
				}
				accountUpdates := map[string]any{"updated_at": now}
				mailboxUpdates := map[string]any{"updated_at": now}
				if emailChanged {
					var mailbox SunnyMailbox
					mailboxErr := tx.Where("LOWER(email) = ?", sunnyEmailKey(originalEmail)).First(&mailbox).Error
					if mailboxErr == nil {
						mailboxUpdates["email"] = targetEmail
						mailboxUpdates["raw"] = sunnyMailboxRawForEmail(mailbox, targetEmail)
						sess.RawMailboxLine = mailboxUpdates["raw"].(string)
					} else if mailboxErr != gorm.ErrRecordNotFound {
						return mailboxErr
					}
					accountUpdates["email"] = targetEmail
				}
				if _, ok := body["access_token"]; ok {
					sess.AccessToken = text(body["access_token"])
					accountUpdates["access_token"] = sess.AccessToken
				}
				if _, ok := body["refresh_token"]; ok {
					sess.RefreshToken = text(body["refresh_token"])
					accountUpdates["openai_rt"] = sess.RefreshToken
					mailboxUpdates["openai_rt"] = sess.RefreshToken
				}
				if rebindEmailProvided {
					accountUpdates["rebind_email"] = rebindEmail
					mailboxUpdates["rebind_email"] = rebindEmail
				}
				if rebindAPIProvided {
					accountUpdates["rebind_mailbox_api"] = rebindAPI
					mailboxUpdates["rebind_mailbox_api"] = rebindAPI
					if rebindEmail != "" {
						mailboxUpdates["mailbox_type"] = "domain"
						mailboxUpdates["mailbox_channel"] = "domain_api"
						mailboxUpdates["access_key"] = rebindAPI
						mailboxUpdates["pickup_token_hash"] = domainMailboxTokenHashFromCredential(rebindAPI, rebindEmail)
						mailboxUpdates["raw"] = sunnyURLAPIRaw(rebindEmail, rebindAPI)
						sess.RawMailboxLine = mailboxUpdates["raw"].(string)
					}
				}
				if v := text(body["session_json"]); v != "" {
					sess.SessionJSON = v
				}
				if err := tx.Save(&sess).Error; err != nil {
					return err
				}
				if status != "" {
					accountUpdates["status"] = status
					accountUpdates["status_changed_at"] = now
					mailboxUpdates["status"] = status
					mailboxUpdates["status_changed_at"] = now
				}
				if planType != "" {
					accountUpdates["account_type"] = planType
					mailboxUpdates["account_type"] = planType
				}
				if _, ok := body["trial_eligibility"]; ok {
					trialEligibility := normalizeSunnyTrialEligibility(text(body["trial_eligibility"]))
					trialCheckedAt := sunnyManualTrialCheckedAt(trialEligibility)
					accountUpdates["trial_eligibility"] = trialEligibility
					accountUpdates["trial_check_error"] = ""
					accountUpdates["trial_checked_at"] = trialCheckedAt
					mailboxUpdates["trial_eligibility"] = trialEligibility
					mailboxUpdates["trial_check_error"] = ""
					mailboxUpdates["trial_checked_at"] = trialCheckedAt
				}
				if targetGroup != nil {
					accountUpdates["group_name"] = targetGroup.Name
					mailboxUpdates["group_id"] = targetGroup.ID
				}
				accountQuery := tx.Model(&SunnyAccount{})
				if linkedAccount.ID != 0 {
					accountQuery = accountQuery.Where("id = ?", linkedAccount.ID)
				} else {
					accountQuery = accountQuery.Where("LOWER(email) = ?", sunnyEmailKey(originalEmail))
				}
				if err := accountQuery.Updates(accountUpdates).Error; err != nil {
					return err
				}
				mailboxQuery := tx.Model(&SunnyMailbox{})
				if linkedMailbox.ID != 0 {
					mailboxQuery = mailboxQuery.Where("id = ?", linkedMailbox.ID)
				} else {
					mailboxQuery = mailboxQuery.Where("LOWER(email) = ?", sunnyEmailKey(originalEmail))
				}
				mailboxResult := mailboxQuery.Updates(mailboxUpdates)
				if mailboxResult.Error != nil {
					return mailboxResult.Error
				}
				if targetGroup != nil && mailboxResult.RowsAffected == 0 {
					return fmt.Errorf("mailbox not found for session")
				}
				return nil
			}); err != nil {
				writeError(w, 400, err.Error())
				return
			}
			accounts, mailboxes := s.sunnySessionSidecars([]SunnySession{sess})
			writeJSON(w, 200, s.serializeSunnySession(sess, accounts, mailboxes))
			return
		}
		if r.Method == http.MethodDelete {
			s.db.Delete(&sess)
			writeJSON(w, 200, map[string]any{"ok": true})
			return
		}
	}
	writeError(w, 404, "not found")
}

func (s *Server) sunnyExportSessions(w http.ResponseWriter, rows []SunnySession, format string) {
	switch format {
	case "ls":
		lines := []string{}
		_, mailboxes := s.sunnySessionSidecars(rows)
		for _, row := range rows {
			if line := sunnyLoginSecretLine(mailboxes[sunnyEmailKey(row.Email)]); line != "" {
				lines = append(lines, line)
			}
		}
		writeTextFile(w, sunnyAccountExportName("LS", len(lines), "txt"), "text/plain; charset=utf-8", []byte(strings.Join(lines, "\n")+"\n"))
	case "at":
		lines := []string{}
		accounts, _ := s.sunnySessionSidecars(rows)
		for _, row := range rows {
			account := accounts[sunnyEmailKey(row.Email)]
			if token := strings.TrimSpace(sunnyPreferredAccessToken(row.AccessToken, sunnyAccessTokenFromSessionJSON(row.SessionJSON), account.AccessToken)); token != "" {
				lines = append(lines, token)
			}
		}
		writeTextFile(w, sunnyAccountExportName("AT", len(lines), "txt"), "text/plain; charset=utf-8", []byte(strings.Join(lines, "\n")+"\n"))
	case "sk":
		lines := []string{}
		_, mailboxes := s.sunnySessionSidecars(rows)
		for _, row := range rows {
			mailbox := mailboxes[sunnyEmailKey(row.Email)]
			if line := sunnyMailboxCredentialLine(mailbox); line != "" {
				lines = append(lines, line)
			}
		}
		writeTextFile(w, sunnyAccountExportName("SK", len(lines), "txt"), "text/plain; charset=utf-8", []byte(strings.Join(lines, "\n")+"\n"))
	case "sub":
		exportedAccounts := []any{}
		cfg := s.sunnyGetConfig(sunnyCfgSub2API, defaultSub2APIConfig())
		accounts, mailboxes := s.sunnySessionSidecars(rows)
		for _, row := range rows {
			key := sunnyEmailKey(row.Email)
			account := accounts[key]
			row.AccessToken = sunnyPreferredAccessToken(row.AccessToken, sunnyAccessTokenFromSessionJSON(row.SessionJSON), account.AccessToken)
			row.RefreshToken = firstText(row.RefreshToken, account.OpenAIRT)
			row.RawMailboxLine = sunnySessionSecretKey(row, mailboxes[key])
			if strings.TrimSpace(row.AccessToken) == "" {
				continue
			}
			exportedAccounts = append(exportedAccounts, buildSunnySub2AccountPayload(row, cfg, mailboxes[key]))
		}
		payload := map[string]any{"exported_at": formatTime(time.Now()), "proxies": []any{}, "accounts": exportedAccounts}
		writeTextFile(w, sunnyAccountExportName("SUB", len(exportedAccounts), "json"), "application/json", []byte(dumpJSONPretty(payload)+"\n"))
	case "session_json", "json":
		arr := []any{}
		for _, r := range rows {
			if strings.TrimSpace(r.SessionJSON) != "" {
				arr = append(arr, jsonMap(r.SessionJSON))
			}
		}
		writeTextFile(w, timestampName("auth_sessions", "json"), "application/json", []byte(dumpJSONPretty(map[string]any{"items": arr})+"\n"))
	case "access_token":
		lines := []string{}
		accounts, _ := s.sunnySessionSidecars(rows)
		for _, r := range rows {
			lines = append(lines, sunnyPreferredAccessToken(r.AccessToken, sunnyAccessTokenFromSessionJSON(r.SessionJSON), accounts[sunnyEmailKey(r.Email)].AccessToken))
		}
		writeTextFile(w, timestampName("access_tokens", "txt"), "text/plain; charset=utf-8", []byte(strings.Join(lines, "\n")+"\n"))
	case "secret_key", "mailbox_account", "raw":
		lines := []string{}
		_, mailboxes := s.sunnySessionSidecars(rows)
		for _, r := range rows {
			line := ""
			if mb := mailboxes[sunnyEmailKey(r.Email)]; mb.Email != "" {
				line = sunnyMailboxCredentialLine(mb)
			}
			if line == "" {
				line = strings.TrimSpace(r.RawMailboxLine)
			}
			if line != "" {
				lines = append(lines, line)
			}
		}
		writeTextFile(w, timestampName("mailbox_accounts", "txt"), "text/plain; charset=utf-8", []byte(strings.Join(lines, "\n")+"\n"))
	case "all":
		accounts, mailboxes := s.sunnySessionSidecars(rows)
		arr := []any{}
		for _, r := range rows {
			arr = append(arr, s.serializeSunnySession(r, accounts, mailboxes))
		}
		writeTextFile(w, timestampName("session_accounts", "json"), "application/json", []byte(dumpJSONPretty(map[string]any{"items": arr})+"\n"))
	case "sub2api_json":
		arr := []any{}
		cfg := defaultSub2APIConfig()
		_, mailboxes := s.sunnySessionSidecars(rows)
		for _, r := range rows {
			r.RawMailboxLine = sunnySessionSecretKey(r, mailboxes[sunnyEmailKey(r.Email)])
			arr = append(arr, buildSunnySub2AccountPayload(r, cfg, mailboxes[sunnyEmailKey(r.Email)]))
		}
		writeTextFile(w, timestampName("sub2api", "json"), "application/json", []byte(dumpJSONPretty(map[string]any{"accounts": arr})+"\n"))
	default:
		writeTextFile(w, timestampName("sessions", "json"), "application/json", []byte(dumpJSONPretty(rows)+"\n"))
	}
}

func sunnyAccountExportName(prefix string, count int, suffix string) string {
	stamp := time.Now().In(applicationLocation()).Format("20060102150405")
	return fmt.Sprintf("%s-%s-%d.%s", prefix, stamp, count, suffix)
}

func (s *Server) sunnyValidateAcquireRTAccounts(accountIDs []uint) error {
	var accounts []SunnyAccount
	if err := s.db.Select("id", "email", "status", "phone_number").Where("id IN ?", accountIDs).Find(&accounts).Error; err != nil {
		return err
	}
	accountByID := make(map[uint]SunnyAccount, len(accounts))
	emails := make([]string, 0, len(accounts))
	for _, account := range accounts {
		accountByID[account.ID] = account
		emails = append(emails, account.Email)
	}
	mailboxStatus := map[string]string{}
	if len(emails) > 0 {
		var mailboxes []SunnyMailbox
		if err := s.db.Select("email", "status").Where("email IN ?", emails).Find(&mailboxes).Error; err != nil {
			return err
		}
		for _, mailbox := range mailboxes {
			mailboxStatus[sunnyEmailKey(mailbox.Email)] = mailbox.Status
		}
	}
	for _, accountID := range accountIDs {
		account, ok := accountByID[accountID]
		if !ok || !sunnyPhoneBindingCompleted(account.PhoneNumber, account.Status, mailboxStatus[sunnyEmailKey(account.Email)]) {
			return fmt.Errorf("当前账户未接码，请先完成接码后再获取RT")
		}
	}
	return nil
}

func (s *Server) sunnyPrepareAddLSTask(accountIDs []uint) ([]uint, []map[string]any, error) {
	var accounts []SunnyAccount
	if err := s.db.Select("id", "mailbox_id", "email").Where("id IN ?", accountIDs).Find(&accounts).Error; err != nil {
		return nil, nil, err
	}
	accountByID := make(map[uint]SunnyAccount, len(accounts))
	mailboxIDs := make([]uint, 0, len(accounts))
	emails := make([]string, 0, len(accounts))
	for _, account := range accounts {
		accountByID[account.ID] = account
		if account.MailboxID != 0 {
			mailboxIDs = append(mailboxIDs, account.MailboxID)
		}
		if strings.TrimSpace(account.Email) != "" {
			emails = append(emails, account.Email)
		}
	}
	var mailboxes []SunnyMailbox
	query := s.db.Select("id", "email", "chat_gpt_password", "totp_secret")
	switch {
	case len(mailboxIDs) > 0 && len(emails) > 0:
		query = query.Where("id IN ? OR email IN ?", mailboxIDs, emails)
	case len(mailboxIDs) > 0:
		query = query.Where("id IN ?", mailboxIDs)
	case len(emails) > 0:
		query = query.Where("email IN ?", emails)
	default:
		return append([]uint(nil), accountIDs...), nil, nil
	}
	if err := query.Find(&mailboxes).Error; err != nil {
		return nil, nil, err
	}
	mailboxByID := make(map[uint]SunnyMailbox, len(mailboxes))
	mailboxByEmail := make(map[string]SunnyMailbox, len(mailboxes))
	for _, mailbox := range mailboxes {
		mailboxByID[mailbox.ID] = mailbox
		mailboxByEmail[sunnyEmailKey(mailbox.Email)] = mailbox
	}
	eligible := make([]uint, 0, len(accountIDs))
	skipped := make([]map[string]any, 0)
	seen := make(map[uint]bool, len(accountIDs))
	for _, accountID := range accountIDs {
		if seen[accountID] {
			continue
		}
		seen[accountID] = true
		account, ok := accountByID[accountID]
		if !ok {
			eligible = append(eligible, accountID)
			continue
		}
		mailbox, found := mailboxByID[account.MailboxID]
		if !found {
			mailbox, found = mailboxByEmail[sunnyEmailKey(account.Email)]
		}
		if found && strings.TrimSpace(mailbox.ChatGPTPassword) != "" && strings.TrimSpace(mailbox.TOTPSecret) != "" {
			skipped = append(skipped, map[string]any{
				"email": account.Email, "status": "skipped", "login_secret_complete": true,
			})
			continue
		}
		eligible = append(eligible, accountID)
	}
	return eligible, skipped, nil
}

func (s *Server) sunnyTasks(w http.ResponseWriter, r *http.Request, parts []string) {
	if len(parts) != 1 || r.Method != http.MethodPost {
		writeError(w, 404, "not found")
		return
	}
	body, _ := parseBody(r)
	typemap := map[string]string{"register": "sunny_register", "login": "sunny_login", "refresh-session": "sunny_refresh_session", "acquire-rt": "sunny_acquire_rt", "add-ls": "sunny_add_ls", "sub2-import": "sunny_sub2_import"}
	typ := typemap[parts[0]]
	if typ == "" {
		writeError(w, 404, "not found")
		return
	}
	if typ == "sunny_register" {
		identity := strings.ToLower(strings.TrimSpace(text(body["identity"])))
		if identity == "remail" {
			if err := s.validateRemailRegistration(body); err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
		} else if identity == "domain" || identity == "domain_mailbox" || identity == "自建域名邮箱" {
			if err := s.sunnyValidateProxyForRegisterTask(boolValue(body["allow_system_proxy_fallback"], false)); err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
			if err := s.prepareDomainMailboxRegistration(body); err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
		}
		if err := s.sunnyValidateRegisterStageResources(body); err != nil {
			writeError(w, 400, err.Error())
			return
		}
	}
	if typ == "sunny_refresh_session" || typ == "sunny_acquire_rt" || typ == "sunny_add_ls" || typ == "sunny_sub2_import" {
		accountIDs := uintSlice(body["account_ids"])
		if len(accountIDs) == 0 {
			sessionIDs := uintSlice(body["session_ids"])
			if len(sessionIDs) > 0 {
				var sessions []SunnySession
				s.db.Select("id", "account_id", "email").Where("id IN ?", sessionIDs).Find(&sessions)
				accountIDs = make([]uint, 0, len(sessions))
				seen := map[uint]bool{}
				for _, session := range sessions {
					accountID := session.AccountID
					if accountID == 0 {
						var account SunnyAccount
						if s.db.Select("id").Where("email = ?", session.Email).First(&account).Error == nil {
							accountID = account.ID
						}
					}
					if accountID != 0 && !seen[accountID] {
						seen[accountID] = true
						accountIDs = append(accountIDs, accountID)
					}
				}
				body["account_ids"] = accountIDs
			}
		}
		if len(accountIDs) == 0 {
			message := "请选择需要刷新 AT 的账户"
			if typ == "sunny_acquire_rt" {
				message = "请选择需要获取 RT 的账户"
			} else if typ == "sunny_add_ls" {
				message = "请选择需要添加 LS 的账户"
			}
			writeError(w, http.StatusBadRequest, message)
			return
		}
		if typ == "sunny_acquire_rt" {
			if err := s.sunnyValidateAcquireRTAccounts(accountIDs); err != nil {
				writeError(w, http.StatusBadRequest, err.Error())
				return
			}
		} else if typ == "sunny_sub2_import" {
			body["account_ids"] = accountIDs
		} else if typ == "sunny_add_ls" {
			eligible, skipped, err := s.sunnyPrepareAddLSTask(accountIDs)
			if err != nil {
				writeError(w, http.StatusInternalServerError, err.Error())
				return
			}
			body["account_ids"] = eligible
			body["account_ids_explicit"] = true
			body["prefiltered_login_secret_items"] = skipped
			body["selected_account_count"] = len(eligible) + len(skipped)
			body["execution_mode"] = "protocol"
			body["protocol_challenge_strategy"] = "sentinel_protocol"
			body["registration_stage"] = "register_only"
			body["setup_login_secret"] = true
		}
	}
	total := len(uintSlice(body["mailbox_ids"])) + len(uintSlice(body["account_ids"]))
	if typ == "sunny_refresh_session" || typ == "sunny_acquire_rt" || typ == "sunny_add_ls" || typ == "sunny_sub2_import" {
		total = len(uintSlice(body["account_ids"]))
	}
	if typ == "sunny_add_ls" {
		total = intValue(body["selected_account_count"], total)
	}
	switch typ {
	case "sunny_refresh_session":
		body["concurrency"] = s.sunnyATConcurrency()
	case "sunny_add_ls":
		body["concurrency"] = s.sunnyAddLSConcurrency()
	case "sunny_sub2_import":
		body["concurrency"] = s.sunnySub2ImportConcurrency()
	}
	if total == 0 {
		total = intValue(body["count"], 1)
	}
	body = s.sunnyTaskProxySnapshot(body)
	task := s.createTask(typ, "sunny", body, total)
	writeJSON(w, 200, serializeTask(task))
}

func sunnyRegistrationStage(body map[string]any) string {
	stage := strings.ToLower(strings.TrimSpace(firstText(body["registration_stage"], body["stage"])))
	switch stage {
	case "codex_phone_bind", "import_reverse_proxy", "agent_identity_reverse_proxy":
		return stage
	default:
		return "register_only"
	}
}

func sunnySortClause(sortBy string, sortOrder string, allowed map[string]string, fallback string) string {
	col := allowed[strings.ToLower(strings.TrimSpace(sortBy))]
	if col == "" {
		return fallback
	}
	order := strings.ToLower(strings.TrimSpace(sortOrder))
	if order != "asc" {
		order = "desc"
	}
	return col + " " + order
}

func sunnyMailboxListSortClause(sortBy string, sortOrder string) string {
	if strings.EqualFold(strings.TrimSpace(sortBy), "rebind_email") {
		emptyFirst := strings.EqualFold(strings.TrimSpace(sortOrder), "asc")
		emptyRank, valueRank := "1", "0"
		if emptyFirst {
			emptyRank, valueRank = "0", "1"
		}
		valueOrder := "ASC"
		if !emptyFirst {
			valueOrder = "DESC"
		}
		return "CASE WHEN TRIM(COALESCE(rebind_email, '')) = '' THEN " + emptyRank +
			" ELSE " + valueRank + " END ASC, LOWER(TRIM(COALESCE(rebind_email, ''))) " + valueOrder + ", id DESC"
	}
	return sunnySortClause(sortBy, sortOrder, map[string]string{
		"updated_at": "updated_at", "status_changed_at": "status_changed_at",
		"created_at": "created_at", "registered_at": "registered_at",
	}, "id desc")
}

func (s *Server) sunnyValidateRegisterStageResources(body map[string]any) error {
	identity := strings.ToLower(strings.TrimSpace(text(body["identity"])))
	if identity == "remail" || identity == "domain" || identity == "domain_mailbox" || identity == "自建域名邮箱" {
		return s.sunnyValidateProxyForRegisterTask(boolValue(body["allow_system_proxy_fallback"], false))
	}
	mailboxCfg := s.sunnyGetConfig(sunnyCfgMailbox, defaultMailboxConfig())
	if !boolValue(mailboxCfg["pool_enabled"], true) {
		return fmt.Errorf("mailbox config is unavailable: enable the self-managed mailbox pool first")
	}
	mailboxes, err := s.sunnyMailboxesForRegisterTask(body)
	if err != nil {
		return err
	}
	if len(mailboxes) == 0 {
		return fmt.Errorf("mailbox config is unavailable: import and enable at least one mailbox first")
	}
	if err := s.sunnyValidateProxyForRegisterTask(boolValue(body["allow_system_proxy_fallback"], false)); err != nil {
		return err
	}
	// SMS and sub2api are post-registration stages. Missing resources must not
	// block the base ChatGPT registration/login; the Worker records the last
	// completed stage and stops at 已注册 or 已接码 with a detailed log.
	return nil
}

func (s *Server) sunnyRegisterProxyReadiness() map[string]any {
	cfg := s.sunnyGetConfig(sunnyCfgProxy, defaultProxyConfig())
	proxyEnabled := boolValue(cfg["proxy_enabled"], true)
	explicitProxy := normalizeSunnyProxyAddress(text(cfg["register_proxy"]))
	var n int64
	s.db.Model(&SunnyProxy{}).
		Where("status = ? AND enabled = ? AND last_check_ok = ?", "enabled", true, true).
		Where("(',' || replace(lower(coalesce(purpose_tags, '')), ' ', '') || ',') LIKE ?", "%,"+sunnyProxyPurposeRegister+",%").
		Count(&n)
	usable := !proxyEnabled || explicitProxy != "" || n > 0
	return map[string]any{
		"proxy_enabled":               proxyEnabled,
		"usable":                      usable,
		"requires_confirmation":       proxyEnabled && !usable,
		"usable_register_proxies":     n,
		"has_explicit_register_proxy": explicitProxy != "",
		"stats":                       s.sunnyProxyStats(),
	}
}

func (s *Server) sunnyValidateProxyForRegisterTask(allowSystemFallback bool) error {
	readiness := s.sunnyRegisterProxyReadiness()
	if boolValue(readiness["usable"], false) || allowSystemFallback {
		return nil
	}
	stats, _ := readiness["stats"].(map[string]int64)
	return fmt.Errorf("proxy config is enabled but no checked usable proxy is available: total=%d enabled=%d disabled=%d invalid=%d", stats["total"], stats["enabled"], stats["disabled"], stats["invalid"])
}

func (s *Server) sunnyMailboxesForRegisterTask(body map[string]any) ([]SunnyMailbox, error) {
	ids := uintSlice(body["mailbox_ids"])
	var rows []SunnyMailbox
	if len(ids) > 0 {
		s.db.Where("id IN ?", ids).Order("id asc").Find(&rows)
		if len(rows) != len(ids) {
			return nil, fmt.Errorf("mailbox config is unavailable: selected mailbox does not exist")
		}
		seen := map[uint]bool{}
		for _, m := range rows {
			seen[m.ID] = true
			if !m.Enabled {
				return nil, fmt.Errorf("mailbox config is unavailable: selected mailbox is disabled: %s", m.Email)
			}
		}
		for _, id := range ids {
			if !seen[id] {
				return nil, fmt.Errorf("mailbox config is unavailable: selected mailbox does not exist")
			}
		}
		return rows, nil
	}
	query := s.db.Where("enabled = ? AND status NOT IN ?", true, []string{"disabled", "禁用"})
	if count := intValue(body["count"], 0); count > 0 {
		query = query.Limit(count)
	}
	query.Order("id asc").Find(&rows)
	return rows, nil
}

func (s *Server) sunnyMailboxesNeedPhone(rows []SunnyMailbox) bool {
	for _, m := range rows {
		if strings.TrimSpace(m.OpenAIRT) != "" {
			continue
		}
		var account SunnyAccount
		if err := s.db.Select("id", "openai_rt").First(&account, "email = ?", m.Email).Error; err == nil && strings.TrimSpace(account.OpenAIRT) != "" {
			continue
		}
		var session SunnySession
		if err := s.db.Select("id", "refresh_token").First(&session, "email = ?", m.Email).Error; err == nil && strings.TrimSpace(session.RefreshToken) != "" {
			continue
		}
		return true
	}
	return false
}

func (s *Server) sunnyPhoneTotalCount() int64 {
	var n int64
	s.db.Model(&SunnyPhone{}).Count(&n)
	return n
}

func (s *Server) sunnyUsablePhoneCount() int64 {
	cfg := s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig())
	if !boolValue(cfg["pool_enabled"], true) {
		return 0
	}
	var n int64
	s.db.Model(&SunnyPhone{}).
		Where("enabled = ?", true).
		Where("coalesce(status,'available') NOT IN ?", []string{"disabled", "full", "in_use"}).
		Where("coalesce(success_count,0) < coalesce(max_success,3)").
		Where("(cooldown_until IS NULL OR cooldown_until <= ?)", time.Now()).
		Count(&n)
	return n
}

func (s *Server) sunnyHasUsableSMSConfig() bool {
	if s.sunnyUsablePhoneCount() > 0 {
		return true
	}
	cfg := s.sunnyGetConfig(sunnyCfgPhone, defaultPhoneConfig())
	if boolValue(cfg["luban_enabled"], false) && strings.TrimSpace(text(cfg["luban_api_key"])) != "" && strings.TrimSpace(text(cfg["luban_service_id"])) != "" {
		return true
	}
	if boolValue(cfg["smsbower_enabled"], false) && strings.TrimSpace(text(cfg["smsbower_api_key"])) != "" {
		return true
	}
	if boolValue(cfg["smspool_enabled"], false) && strings.TrimSpace(text(cfg["smspool_api_key"])) != "" {
		return true
	}
	maxPrice, _ := strconv.ParseFloat(strings.TrimSpace(text(cfg["firefox_max_price"])), 64)
	return boolValue(cfg["firefox_enabled"], false) &&
		fireFoxAPIToken(cfg) != "" &&
		strings.TrimSpace(text(cfg["firefox_default_country"])) != "" &&
		strings.TrimSpace(text(cfg["firefox_default_service"])) != "" &&
		maxPrice > 0
}

func (s *Server) sunnyProxyStats() map[string]int64 {
	var allTotal, enabledTotal, disabledTotal, invalidTotal int64
	s.db.Model(&SunnyProxy{}).Count(&allTotal)
	s.db.Model(&SunnyProxy{}).Where("status = ? AND enabled = ?", "enabled", true).Count(&enabledTotal)
	s.db.Model(&SunnyProxy{}).Where("status = ?", "disabled").Count(&disabledTotal)
	s.db.Model(&SunnyProxy{}).Where("status = ? OR (last_check_ok = ? AND last_checked_at IS NOT NULL)", "invalid", false).Count(&invalidTotal)
	return map[string]int64{"total": allTotal, "enabled": enabledTotal, "disabled": disabledTotal, "invalid": invalidTotal}
}

func (s *Server) sunnyTaskProxySnapshot(payload map[string]any) map[string]any {
	next := map[string]any{}
	for k, v := range payload {
		next[k] = v
	}
	cfg := s.sunnyGetConfig(sunnyCfgProxy, defaultProxyConfig())
	stats := s.sunnyProxyStats()
	proxyEnabled := boolValue(cfg["proxy_enabled"], true)
	localProxy := normalizeSunnyProxyAddress(fallback(text(cfg["local_proxy"]), "http://127.0.0.1:7897"))
	next["proxy_enabled"] = proxyEnabled
	next["proxy_stats"] = stats
	browserTrafficConfig := map[string]any{}
	if raw, ok := cfg["browser_traffic_optimization"].(map[string]any); ok {
		browserTrafficConfig = raw
	}
	next["browser_traffic_optimization"] = mergeConfig(
		defaultProxyConfig()["browser_traffic_optimization"].(map[string]any),
		browserTrafficConfig,
	)
	next["local_proxy"] = localProxy
	if !proxyEnabled {
		next["register_proxy"] = ""
		next["proxy"] = ""
		next["system_proxy"] = normalizeSunnyProxyAddress(text(cfg["system_proxy"]))
		return next
	}
	next["system_proxy"] = localProxy
	registerProxy := normalizeSunnyProxyAddress(text(cfg["register_proxy"]))
	explicitProxy := registerProxy
	var proxies []SunnyProxy
	s.db.Where("status = ? AND enabled = ? AND last_check_ok = ?", "enabled", true, true).
		Where("(',' || replace(lower(coalesce(purpose_tags, '')), ' ', '') || ',') LIKE ?", "%,"+sunnyProxyPurposeRegister+",%").
		Order("updated_at desc, id asc").Find(&proxies)
	proxyPool := make([]string, 0, len(proxies))
	proxyIDs := make([]uint, 0, len(proxies))
	for _, p := range proxies {
		address := normalizeSunnyProxyAddress(p.Address)
		if address == "" {
			continue
		}
		proxyPool = append(proxyPool, address)
		proxyIDs = append(proxyIDs, p.ID)
	}
	if len(proxyPool) > 0 {
		registerProxy = proxyPool[0]
		next["proxy_pool"] = proxyPool
		next["proxy_ids"] = proxyIDs
		next["proxy_pool_size"] = len(proxyPool)
	}
	if registerProxy == "" && len(proxyPool) == 0 && explicitProxy == "" && boolValue(payload["allow_system_proxy_fallback"], false) {
		next["proxy_enabled"] = false
		next["proxy_pool_fallback_confirmed"] = true
		next["register_proxy"] = ""
		next["proxy"] = ""
		next["system_proxy"] = normalizeSunnyProxyAddress(text(cfg["system_proxy"]))
		delete(next, "proxy_pool")
		delete(next, "proxy_ids")
		delete(next, "proxy_pool_size")
		return next
	}
	next["local_proxy"] = localProxy
	next["register_proxy"] = registerProxy
	next["proxy"] = registerProxy
	return next
}

func (s *Server) sunnyHasUsableMailbox(body map[string]any) bool {
	rows, err := s.sunnyMailboxesForRegisterTask(body)
	return err == nil && len(rows) > 0
}

func (s *Server) sunnyImportState(w http.ResponseWriter, r *http.Request) {
	body, _ := parseBody(r)
	path := strings.TrimSpace(text(body["path"]))
	if path == "" {
		writeError(w, 400, "璇锋彁渚?state.json 璺緞")
		return
	}
	b, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		writeError(w, 400, err.Error())
		return
	}
	var state map[string]any
	if json.Unmarshal(b, &state) != nil {
		writeError(w, 400, "state.json 鏍煎紡閿欒")
		return
	}
	imported := 0
	if arr, ok := state["accounts"].([]any); ok {
		gid := s.sunnyEnsureDefaultGroup()
		for _, raw := range arr {
			if m, ok := raw.(map[string]any); ok {
				line := text(m["raw"])
				if line == "" {
					line = strings.Join([]string{text(m["email"]), text(m["password"]), text(m["client_id"]), text(m["refresh_token"])}, "----")
				}
				if p, err := parseSunnyMailboxLine(line); err == nil {
					canonicalRaw := sunnyMicrosoftRaw(p["email"], p["password"], p["client_id"], p["refresh_token"])
					mb := SunnyMailbox{GroupID: gid, Email: p["email"], Password: p["password"], ClientID: p["client_id"], RefreshToken: p["refresh_token"], OpenAIRT: fallback(text(m["openai_rt"]), p["openai_rt"]), Raw: canonicalRaw, AccountType: fallback(text(m["account_type"]), "free"), Status: fallback(text(m["status"]), "unused"), Enabled: true, LatestMailJSON: "{}"}
					s.db.FirstOrCreate(&mb, SunnyMailbox{Email: mb.Email})
					imported++
				}
			}
		}
	}
	if arr, ok := state["phones"].([]any); ok {
		for _, raw := range arr {
			if m, ok := raw.(map[string]any); ok {
				p := SunnyPhone{Number: text(m["number"]), SmsURL: text(m["sms_url"]), Status: "available", Enabled: true, SuccessCount: intValue(m["receive_count"], 0), MaxSuccess: 3}
				if p.Number != "" {
					s.db.FirstOrCreate(&p, SunnyPhone{Number: p.Number})
				}
			}
		}
	}
	writeJSON(w, 200, map[string]any{"ok": true, "imported_mailboxes": imported})
}

func (s *Server) sunnyGetConfig(key string, def map[string]any) map[string]any {
	var row SunnyKVConfig
	if s.db.First(&row, "key = ?", key).Error != nil {
		return def
	}
	return mergeConfig(def, jsonMap(row.ValueJSON))
}
func (s *Server) sunnySaveConfig(key string, value map[string]any) {
	row := SunnyKVConfig{Key: key, ValueJSON: dumpJSON(value)}
	s.db.Save(&row)
}
func mergeConfig(base map[string]any, over map[string]any) map[string]any {
	out := map[string]any{}
	for k, v := range base {
		out[k] = v
	}
	for k, v := range over {
		out[k] = v
	}
	return out
}
func firstText(values ...any) string {
	for _, v := range values {
		if t := text(v); t != "" {
			return t
		}
	}
	return ""
}
func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
