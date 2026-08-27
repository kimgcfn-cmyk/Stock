function createLunchSurvey() {
  var form = FormApp.create('점심 메뉴 설문')
    .setDescription('오늘 점심 메뉴를 투표해 주세요!')
    .setCollectEmail(false);

  var nameItem = form.addTextItem();
  nameItem.setTitle('이름')
    .setRequired(true);

  var menuItem = form.addCheckboxItem();
  menuItem.setTitle('오늘 점심 메뉴를 선택해 주세요 (1개 이상)')
    .setChoiceValues([
      '김치찌개',
      '된장찌개',
      '비빔밥',
      '김치볶음밥',
      '제육볶음',
      '불고기',
      '돈까스',
      '우동',
      '짬뽕',
      '짜장면',
      '칼국수',
      '냉면',
      '샌드위치',
      '파스타',
      '피자',
      '치킨',
      '샐러드',
      '도시락',
      '국밥',
      '떡볶이'
    ])
    .setRequired(true)
    .setHelpText('원하는 메뉴가 없다면 기타 의견에 적어 주세요.');

  form.addParagraphTextItem()
    .setTitle('기타 의견 (원하는 메뉴 제안 등)')
    .setRequired(false);

  Logger.log('폼 URL: ' + form.getPublishedUrl());
  Logger.log('편집 URL: ' + form.getEditUrl());
}
