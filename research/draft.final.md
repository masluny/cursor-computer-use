# Agenci computer use: co działa i co z tego wziąć do cursor-computer-use

> Przegląd badań nad agentami sterującymi komputerem przez interfejs graficzny, z listą ulepszeń dla serwera MCP, który daje Grokowi w Cursorze dostęp do Maca.

## Percepcja: obraz czy struktura interfejsu

W benchmarku OSWorld sam zrzut ekranu dawał najsilniejszym modelom wizyjnym tylko kilka procent skuteczności, a autorzy podkreślają, że drzewo dostępności odegrało decydującą rolę [1, p. 13]. Ten sam kierunek widać w agentach webowych: najlepsza strategia w SeeAct łączy strukturę HTML z obrazem [2, abstract]. Agent Navi osiągnął najlepszy wynik 19.5% w Windows Agent Arena dzięki połączeniu znaczników, drzewa dostępności systemu i detektorów elementów z pikseli [3, p. 2]. Agent S używa obrazu do śledzenia zmian, a drzewa dostępności do precyzyjnego wskazywania elementów [4, p. 2]. UFO2 łączy Windows UI Automation z parsowaniem obrazu [5, abstract].

Struktura ma jednak słabe strony. Autorzy UGround zwracają uwagę, że drzewa dostępności wnoszą szum, braki i dodatkowy koszt [6, abstract]. Przegląd metod efektywności opisuje je jako zwarte, ale często niekompletne lub niezgodne z tym, co widać na ekranie [7, p. 5]. Gdy struktura i obraz sobie przeczą, modele potrafią nadmiernie ufać błędnej strukturze [8, p. 15].

Ogólne modele wizyjno-językowe wciąż słabo trafiają w elementy. Najlepsze strategie w SeeAct zostawały o 20-25% za idealnym wskazywaniem [2, p. 8]. Na ekranach profesjonalnych o wysokiej rozdzielczości najlepszy model w ScreenSpot-Pro osiągnął tylko 18.9% [9, abstract]. Pomagają znaczniki nałożone na obraz [10, p. 2] i parsowanie zrzutu na elementy [11, p. 1]. Wynik nie jest jednak jednoznaczny: w agentach webowych same znaczniki okazały się nieskuteczne [2, abstract].

Najprostszy sposób na lepsze trafianie bez trenowania modelu to zawężenie obszaru. ScreenSpot-Pro pokazał, że ograniczenie obszaru poszukiwań poprawia dokładność [9, abstract]. W eksperymentach GUI-Lens, z kadrowaniem od ogółu do szczegółu dla modeli ogólnego przeznaczenia, wyniki były wyższe nawet o 24.9 punktu procentowego [12, p. 2]. DRS-GUI uzyskał stałe zyski zwłaszcza na gęstych interfejsach w wysokiej rozdzielczości [13, p. 8]. UI-Zoomer opiera się na tej samej obserwacji: przycięcie i ponowne wskazanie w wyższej rozdzielczości wiąże się z dokładniejszą lokalizacją [14, abstract]. Wyższa rozdzielczość zrzutu sama w sobie zwykle pomaga [1, p. 13].

## Szybkość: mniej wywołań modelu

Agenci są wolni głównie przez wywołania dużego modelu. Badanie OSWorld-Human przypisuje większość opóźnienia planowaniu, refleksji i ocenie wykonywanym przez duży model [15, abstract]. Nawet najlepsze z 16 badanych agentów wykonywały 2.7-4.3 razy więcej kroków niż potrzeba [15, p. 1]. Każdy krok to osobne wywołanie modelu, więc mniej kroków to mniejsze opóźnienie i koszt [5, p. 19]. Mniej myślenia nie musi jednak przyspieszać pracy: cua-speedrun odnotował, że więcej rozumowania może skrócić czas zadania, bo agent unika powtarzania nieudanych akcji [16, p. 9].

Dwie techniki skracają liczbę kroków. Pierwsza to kilka akcji w jednym wywołaniu: w pomiarach UFO2 koszt wnioskowania był niższy nawet o 51.5% bez utraty niezawodności [5, p. 14]. Druga to akcje przez API lub narzędzia zamiast klikania. Według autorów UFO2 natywne API wiązały się ze skutecznością wyższą o ponad 8% i mniejszą kruchością niż samo klikanie [5, p. 14]. UltraCUA z akcjami hybrydowymi był o 22% skuteczniejszy względnie i o 11% szybszy [17, p. 1]. W OSWorld-MCP narzędzia MCP podniosły skuteczność na przykład z 8.3% do 20.4% dla OpenAI o3 [18, abstract].

Narzędzia nie działają jednak same z siebie. Nawet najmocniejsze modele sięgały po nie w 36.3% przypadków [18, abstract]. W tej samej konfiguracji wynik modelu rozumującego wzrósł o +4.0pp, a modelu bez rozumowania spadł o -5.9pp [19, abstract]. ToolCUA opisuje niepewność agentów co do tego, kiedy klikać, a kiedy użyć narzędzia [20, abstract].

Największe oszczędności przy powtarzalnych zadaniach daje ponowne użycie udanych przebiegów. W eksperymentach EchoPath, który odtwarza zweryfikowane sekwencje akcji, mediana kosztu tokenów była niższa o ponad 90%, a mediana czasu o około 60% [21, abstract]. Pamięć przepływów AWM dała względny wzrost skuteczności o 24.6% i 51.1% [22, p. 11]. Polityki neuro-symboliczne wiązały się z większą niezawodnością powtarzanych zadań oraz niższym kosztem i opóźnieniem każdego przebiegu [23, p. 11]. Odtwarzanie wymaga jednak zabezpieczeń: EchoPath odrzuca niejednoznaczne kroki i wraca do naprawy wskazań albo do planowania od nowa [21, abstract].

## Niezawodność i macOS

Na macOS agenci wciąż często zawodzą. W macOSWorld najlepsze systemy komercyjne przekraczały 30% skuteczności [24, abstract], a w MacArena najlepszy wynik wyniósł 31.83% [25, p. 6]. Analiza błędów GUITAR pokazuje, że większość pomyłek to nieukończenie zadania, błędne postrzeganie stanu, złe zrozumienie instrukcji i brak wiedzy o interakcji [26, p. 20]. Według przytaczanych danych 56.7% akcji agentów trafia w zły element [27, p. 2], a stan ekranu może się zmienić między zrzutem a kliknięciem [27, p. 3].

## Bezpieczeństwo

Treści widoczne na ekranie łatwo przejmują agentów. Spreparowane pop-upy skłaniały je do kliknięcia w 86% przypadków, a skuteczność zadań była wtedy niższa o 47% [28, abstract]. Proste obrony, takie jak polecenie ignorowania pop-upów, nie działały [28, abstract]. W OS-Harm wszystkie modele często wykonywały polecenia nadużycia i były podatne na wstrzyknięte polecenia [29, abstract]. Ataki rozłożone na kilka kroków zachowują skuteczność lepiej niż jednokrokowe [30, p. 8]. Autorzy macOSWorld piszą, że ataki przez zwodzenie są ogólne i wymagają pilnej uwagi [24, abstract], a systematyzacja podatności wskazuje, że wiele zagrożeń wynika z architektury, nie tylko z zachowania modelu [31, p. 1].

Najmocniejsze gwarancje daje izolacja architektoniczna, czyli oddzielenie zaufanego planu zadania od niezaufanych obserwacji [32, abstract]. Praktyczny wariant to strażnik działający poza agentem, który sprawdza każde kliknięcie razem ze zrzutem [27, p. 4]. Ograniczenie jest takie, że agent musi stale obserwować ekran, co kłóci się z pełną izolacją [32, abstract]. Szkoda może też powstać przy całkiem niewinnym poleceniu użytkownika [33, abstract].

## Tabela dowodów

**Macierz dowodów (twierdzenie × źródło)**

| Źródło | C1 | C2 | C3 | C4 | C5 | C6 | C7 | C8 | C9 | C10 |
|---|---|---|---|---|---|---|---|---|---|---|
| [1] | ✔ |  | ✔ |  |  |  |  |  |  |  |
| [2] | ✔ | ✘ |  |  |  |  |  |  |  |  |
| [3] | ✔ |  |  |  |  |  |  |  |  |  |
| [4] | ✔ |  |  |  |  |  |  |  |  |  |
| [5] | ✔ |  |  | ✔ | ✔ |  |  |  |  |  |
| [6] | ✘ |  |  |  |  |  |  |  |  |  |
| [7] | ≈ |  |  |  |  |  |  |  |  |  |
| [8] | ≈ |  |  |  |  |  |  |  |  |  |
| [10] |  | ✔ |  |  |  |  |  |  |  |  |
| [11] |  | ✔ |  |  |  |  |  |  |  |  |
| [9] |  | ✔ | ✔ |  |  |  |  |  |  |  |
| [12] |  |  | ✔ |  |  |  |  |  |  |  |
| [13] |  |  | ✔ |  |  |  |  |  |  |  |
| [14] |  |  | ✔ |  |  |  |  |  |  |  |
| [15] |  |  |  | ✔ |  |  |  |  |  |  |
| [16] |  |  |  | ≈ |  |  |  |  |  |  |
| [17] |  |  |  |  | ✔ |  |  |  |  |  |
| [18] |  |  |  |  | ≈ |  |  |  |  |  |
| [19] |  |  |  |  | ≈ |  |  |  |  |  |
| [20] |  |  |  |  | ✘ |  |  |  |  |  |
| [21] |  |  |  |  |  | ≈ |  |  |  |  |
| [22] |  |  |  |  |  | ✔ |  |  |  |  |
| [23] |  |  |  |  |  | ✔ |  |  |  |  |
| [24] |  |  |  |  |  |  | ✔ |  | ✔ |  |
| [25] |  |  |  |  |  |  | ✔ |  |  |  |
| [26] |  |  |  |  |  |  |  | ✔ |  |  |
| [27] |  |  |  |  |  |  |  | ✔ |  | ✔ |
| [28] |  |  |  |  |  |  |  |  | ✔ |  |
| [29] |  |  |  |  |  |  |  |  | ✔ |  |
| [30] |  |  |  |  |  |  |  |  | ✔ |  |
| [31] |  |  |  |  |  |  |  |  | ✔ |  |
| [32] |  |  |  |  |  |  |  |  |  | ≈ |
| [33] |  |  |  |  |  |  |  |  |  | ≈ |

*Twierdzenia: C1 = Połączenie zrzutu ekranu ze strukturą interfejsu (drzewem dostępności) daje agentom lepsze wyniki niż sam obraz | C2 = Ogólne modele wizyjno-językowe słabo wskazują elementy na ekranie; znaczniki i parsowanie ekranu pomagają, ale nie zamykają luki | C3 = Zawężanie obszaru i powiększanie fragmentów ekranu poprawia trafność wskazywania bez trenowania modelu, zwłaszcza przy wysokiej rozdzielczości | C4 = Głównym źródłem powolności agentów są wywołania dużego modelu i nadmiarowe kroki | C5 = Kilka akcji w jednym wywołaniu modelu oraz akcje przez API lub narzędzia zamiast klikania zmniejszają liczbę kroków bez utraty skuteczności | C6 = Ponowne użycie udanych przebiegów (pamięć przepływów, odtwarzanie) obniża koszt i czas powtarzalnych zadań | C7 = Na macOS obecni agenci często zawodzą: najlepsze systemy komercyjne osiągają około 30% skuteczności | C8 = Znaczna część błędów agentów to błędy postrzegania stanu i trafiania we właściwy element | C9 = Treści na ekranie, takie jak pop-upy i wstrzyknięte polecenia, skutecznie przejmują agentów, a proste instrukcje typu ignoruj nie wystarczają | C10 = Najsilniejsze obrony są architektoniczne: oddzielenie zaufanego planu od niezaufanych obserwacji i kontrola akcji poza modelem*


## Wnioski

Nasz serwer już stosuje to, co badania wskazują jako najskuteczniejsze: hybrydę drzewa dostępności z obrazem i OCR, akcje po etykiecie zamiast celowania w piksele, łączenie kroków w batch oraz deep linki zamiast klikania. Kolejne ulepszenia według spodziewanego zysku:

1. **Przepisy, czyli zapamiętane przebiegi.** Po udanym zadaniu zapisać sekwencję akcji semantycznych (click_text, fill, menu, open_url) z punktami kontrolnymi wait_for. Przy powtórce odtworzyć ją jednym wywołaniem, a przy pierwszej niezgodności oddać sterowanie modelowi. To ulepszenie z najmocniejszym efektem w badaniach dla zadań powtarzalnych [21, abstract].
2. **Sprawdzanie celu przed kliknięciem.** Przy click(x, y) odczytać przez Accessibility element pod punktem i zgłosić, w co kliknięcie trafi lub trafiło. Chroni to przed pomyłkami i przed zmianą ekranu między zrzutem a kliknięciem [27, p. 3].
3. **Potwierdzenie poza modelem dla ryzykownych akcji.** Kliknięcia w elementy typu Wyślij, Kup, Usuń, Zapłać zatrzymuje serwer, a nie model, i pyta użytkownika natywnym oknem. Zgodnie z badaniami same instrukcje dla modelu nie wystarczają [28, abstract].
4. **Zoom do klikania.** Zoom ma zwracać fragment z siatką opisaną we współrzędnych pełnego zrzutu, żeby model mógł kliknąć dokładnie w to, co widzi w powiększeniu (podejście od ogółu do szczegółu) [9, abstract].
5. **Narzędzia aplikacji zamiast klikania.** Dla aplikacji skryptowalnych (Muzyka, Notatki, Przypomnienia, Safari, Finder) dodać gotowe akcje przez AppleScript, a w skillu jasno opisać, kiedy po nie sięgać, bo modele same rzadko korzystają z narzędzi [18, abstract].
6. **Zostawić Groka w trybie rozumowania.** Narzędzia pomagają modelom rozumującym, a szkodzą tym bez rozumowania, a więcej myślenia może skrócić całe zadanie [19, abstract]. To odwraca wcześniejszą sugestię, żeby obniżyć poziom myślenia.

## Bibliografia

1. T. Xie et al., "OSWorld: Benchmarking Multimodal Agents for Open-Ended Tasks in Real Computer Environments," *arXiv (Cornell University)*, pp. 52040-52094, 2024, doi:10.48550/arxiv.2404.07972.
2. B. Zheng, B. Gou, J. Kil, H. Sun, and Y. Su, "GPT-4V(ision) is a Generalist Web Agent, if Grounded," *arXiv (Cornell University)*, 2024, doi:10.48550/arxiv.2401.01614.
3. R. Bonatti et al., "Windows Agent Arena: Evaluating Multi-Modal OS Agents at Scale," *arXiv (Cornell University)*, 2024, doi:10.48550/arxiv.2409.08264.
4. S. Agashe, J. Han, S. Gan, J. Yang, A. Li, and X. E. Wang, "Agent S: An Open Agentic Framework that Uses Computers Like a Human," *arXiv*, 2024, arXiv:2410.08164.
5. C. Zhang et al., "UFO2: The Desktop AgentOS," *arXiv (Cornell University)*, 2025, doi:10.48550/arxiv.2504.14603.
6. B. Gou et al., "Navigating the Digital World as Humans Do: Universal Visual Grounding for GUI Agents," *arXiv (Cornell University)*, 2024, doi:10.48550/arxiv.2410.05243.
7. B. Bai et al., "Efficient GUI Agents: A Systems Survey of Observation, Memory, Action, and Runtime Optimization," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2609.02309.
8. G. Zhang, Y. Chen, Y. Qi, and H. Yang, "Do GUI Agents Believe Their Eyes? Diagnosing State-Belief Reliance on Pixels versus Structure," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2607.04334.
9. K. Li et al., "ScreenSpot-Pro: GUI Grounding for Professional High-Resolution Computer Use," *Proceedings of the 33rd ACM International Conference on Multimedia*, pp. 8778-8786, 2025, doi:10.1145/3746027.3755688.
10. J. Yang, H. Zhang, F. Li, X. Zou, C. Li, and J. Gao, "Set-of-Mark Prompting Unleashes Extraordinary Visual Grounding in GPT-4V," *arXiv (Cornell University)*, 2023, doi:10.48550/arxiv.2310.11441.
11. Y. Lü, J. Yang, Y. Shen, and A. H. Awadallah, "OmniParser for Pure Vision Based GUI Agent," *arXiv (Cornell University)*, 2024, doi:10.48550/arxiv.2408.00203.
12. Z. Fu et al., "GUI-Lens: Coarse-to-Fine Cropping for GUI Grounding with General-Purpose VLMs," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2608.03270.
13. Y. Liu, H. Shen, L. Yu, S. Liu, Z. Chen, and Y. Zhou, "DRS-GUI: Dynamic Region Search for Training-Free GUI Grounding," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2605.15542.
14. F. Tang et al., "UI-Zoomer: Uncertainty-Driven Adaptive Zoom-In for GUI Grounding," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2604.14113.
15. R. Abhyankar, Q. Qi, and Y. Zhang, "OSWorld-Human: Benchmarking the Efficiency of Computer-Use Agents," *arXiv (Cornell University)*, 2025, doi:10.48550/arxiv.2506.16042.
16. P. Aggarwal, L. K. Jang, S. Welleck, D. Fried, R. Salakhutdinov, and J. Y. Koh, "cua-speedrun: Standardized Benchmarking of the Speed of Computer-Use Agents," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2609.40284.
17. Y. Yang et al., "UltraCUA: A Foundation Model for Computer Use Agents with Hybrid Action," *arXiv (Cornell University)*, 2025, doi:10.48550/arxiv.2510.17790.
18. H. Jia et al., "OSWorld-MCP: Benchmarking MCP Tool Invocation In Computer-Use Agents," *arXiv*, 2025, arXiv:2510.24563.
19. S. Fan et al., "Screenshots or Tools? Eliciting Tool Use and Managing Multimodal Context in Hybrid GUI-MCP Computer-Use Agents," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2608.03327.
20. X. Hu et al., "ToolCUA: Towards Optimal GUI-Tool Path Orchestration for Computer Use Agents," *arXiv*, 2026, arXiv:2605.12481.
21. Y. Zhao, A. Shanmugham, S. Roy, and Y. Xu, "EchoPath: Execution-Level Replayable Memory for GUI Agents," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2609.16635.
22. Z. Z. Wang, J. Mao, D. Fried, and G. Neubig, "Agent Workflow Memory," *arXiv (Cornell University)*, 2024, doi:10.48550/arxiv.2409.07429.
23. H. Suh et al., "Neuro-Symbolic Computer Use: Learning Reusable Policies for Reliable and Efficient Execution," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2609.36927.
24. P. Yang, H. Ci, and M. Z. Shou, "macOSWorld: A Multilingual Interactive Benchmark for GUI Agents," *neural information processing systems*, pp. 148529-148571, 2025, doi:10.52202/085713-4468.
25. V. Muryn, M. Shamrai, S. Mazepa, and Y. Khodysko, "MacArena: Benchmarking Computer Use Agents on an Online macOS Environment," *arXiv*, 2026, doi:10.48550/arxiv.2606.06560.
26. S. Zhang et al., "GUITAR: Structured Failure Diagnosis of GUI Agents via State Transitions," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2609.34113.
27. X. Liu, B. He, X. Liu, A. Luo, H. Zhang, and H. Chen, "Visual Confused Deputy: Exploiting and Defending Perception Failures in Computer-Using Agents," *arXiv*, 2026, arXiv:2603.14707.
28. Y. Zhang, T. Yu, and D. Yang, "Attacking Vision-Language Computer Agents via Pop-ups," *arXiv (Cornell University)*, pp. 8387-8401, 2024, doi:10.48550/arxiv.2411.02391.
29. T. Kuntz et al., "OS-Harm: A Benchmark for Measuring Safety of Computer Use Agents," *neural information processing systems*, pp. 50396-50427, 2025, doi:10.52202/085713-1502.
30. Z. Zhan, A. Rafiey, A. Ma, L. Pishdad, and L. El Asri, "StepJack: Benchmarking Computer-Use Agent Safety Against Multi-Step Indirect Prompt Injection," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2608.06477.
31. D. J. Jones et al., "A Systematization of Security Vulnerabilities in Computer Use Agents," *arXiv (Cornell University)*, 2025, doi:10.48550/arxiv.2507.05445.
32. H. Foerster et al., "CaMeLs Can Use Computers Too: System-level Security for Computer Use Agents," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2601.09923.
33. X. Ding et al., "The Blind Spot of Agent Safety: How Benign User Instructions Expose Critical Vulnerabilities in Computer-Use Agents," *arXiv (Cornell University)*, 2026, doi:10.48550/arxiv.2604.10577.
