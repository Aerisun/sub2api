package config

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestLoadUsageLogExcludedAPIKeys(t *testing.T) {
	for _, tc := range []struct {
		name    string
		yaml    string
		want    []int64
		invalid bool
	}{
		{name: "default", yaml: "server:\n  mode: debug\n", want: []int64{}},
		{name: "monitor key", yaml: "gateway:\n  usage_record:\n    excluded_api_key_ids: [16]\n", want: []int64{16}},
		{name: "nonpositive key", yaml: "gateway:\n  usage_record:\n    excluded_api_key_ids: [0]\n", invalid: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resetViperWithJWTSecret(t)
			dir := t.TempDir()
			require.NoError(t, os.WriteFile(filepath.Join(dir, "config.yaml"), []byte(tc.yaml), 0o600))
			t.Setenv("DATA_DIR", dir)
			cfg, err := Load()
			if tc.invalid {
				require.ErrorContains(t, err, "excluded_api_key_ids")
				return
			}
			require.NoError(t, err)
			require.Equal(t, tc.want, cfg.Gateway.UsageRecord.ExcludedAPIKeyIDs)
		})
	}
}
