use callit_escrow::{bet_address, vault_address};
use solana_sdk::pubkey::Pubkey;
use std::str::FromStr;
#[test]
fn js_and_rust_agree_on_addresses() {
    let pid = Pubkey::from_str("9fhzt61u3YzUWYRY66E3ArvJVS2We8jvaHVuSLuUdT9Q").unwrap();
    let bet = bet_address(&pid, 7).0;
    assert_eq!(bet.to_string(), "BpqAaXfLEhKTJqE9XkkYqtQM3xGLZr9RRnWDEetkCfPz");
    assert_eq!(vault_address(&pid, &bet).0.to_string(), "2yXE49gYF264HMjftyJTwJDzB4E87c6jaougoqaAUQX5");
}
