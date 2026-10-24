# Discord Music Bot (YouTube)

Apna khud ka Discord music bot — YouTube se gaane bajata hai. Node.js ki zaroorat nahi; **Python + uv** se chalta hai.

---

## Bot ke commands

| Command | Kaam |
| --- | --- |
| `/play <naam ya link>` | YouTube se gaana bajao (queue me add karta hai) |
| `/skip` | Current gaana skip |
| `/stop` | Music band + voice channel chhod do |
| `/pause` | Gaana rok do |
| `/resume` | Ruka gaana wapas chalao |
| `/np` | Abhi kya chal raha hai |
| `/queue` | Aage ke gaane dikhao |
| `/volume <0-200>` | Volume set karo |

---

## Setup (ek hi baar)

### Step 1 — Discord app banao
1. Kholo: https://discord.com/developers/applications
2. **New Application** dabao → naam do (jaise `MyMusicBot`) → **Create**.

### Step 2 — Bot token lo
1. Left menu me **Bot** pe jao.
2. **Reset Token** dabao → **Yes, do it**.
3. Jo token dikhe (`...` lamba) usko **Copy** karo. (Sirf ek baar dikhta hai!)

### Step 3 — Bot ko apne server me invite karo
1. Left menu me **OAuth2** → **OAuth2 URL Generator**.
2. **Scopes** me tick karo:
   - `bot`
   - `applications.commands`
3. **Bot Permissions** me tick karo:
   - `Connect`
   - `Speak`
   - `Send Messages`
   - `Embed Links`
4. Neeche bana **Generated URL** copy karo → browser me kholo → apna server select karo → **Authorize**.

### Step 4 — Token file me daalo
Is folder me `.env` naam ki file banao (ya `.env.example` ko copy karke `.env` banao) aur usme likho:

```
DISCORD_TOKEN=yahan_apna_token_paste_karo
```

Token paste karke save karo. (`.env` gitignore me hai — kahi leak nahi hoga.)

### Step 5 — Bot chalao
Is folder me terminal kholo aur chalao:

```
uv run bot.py
```

**Pehli baar** thoda rukna — `static-ffmpeg` ek baar ffmpeg download karta hai (kuch MB). Uske baad log me ye dikhega:

```
Logged in as MyMusicBot#1234
Bot ready. Use /play in a server.
```

Bas! Ab Discord server me ek **voice channel join karo**, phir text channel me likho:

```
/play kesariya
```

ya koi YouTube link:

```
/play https://www.youtube.com/watch?v=....
```

---

## Roz use karne ke liye
Bas is folder me jao aur chalao:
```
uv run bot.py
```
Bot tabhi online rahega jab tak ye terminal window chalu hai (apne PC pe host ho raha hai). Band karne ke liye `Ctrl + C`.

---

## Agar dikkat aaye

- **`DISCORD_TOKEN nahi mila`** → `.env` file sahi jagah (isi folder me) hai? Token paste kiya? File ka naam exactly `.env` hai (`.env.txt` nahi)?
- **Bot online nahi dikhta** → token sahi hai? Step 3 me bot invite kiya? Terminal me koi red error hai?
- **`/play` pe gaana nahi chalta** → tum voice channel me ho? Server me bot ko voice channel me aane ki permission (Connect/Speak) hai?
- **Commands (`/play`) dikh hi nahi rahe** → Step 3 me `applications.commands` scope tick kiya tha? Bot ko server me invite karne ke baad 1-2 min lag sakte hain.
- **YouTube chalna band kar de** → YouTube free extraction kabhi-kabhi badal jata hai. Fix: `uv lock --upgrade-package yt-dlp` chala ke yt-dlp update karo, phir bot restart karo.

---

## Note
Ye bot YouTube se audio stream karta hai — ye YouTube ki Terms of Service ke khilaf hai. Apne chhote/private server pe seekhne-mazze ke liye theek hai; bade/public use ke liye soch-samajh ke.
